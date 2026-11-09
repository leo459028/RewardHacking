from contextlib import asynccontextmanager
from time import perf_counter
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routers.benchmark import router as benchmark_router
from app.api.routers.evaluations import router as evaluations_router
from app.api.routers.finops import router as finops_router
from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infra.postgres import close_engine, init_engine
from app.services import judge_activation
from app.services.evaluation.judges.profiles import load_profiles
from app.observability.metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL
from app.ui.routers.benchmark import router as benchmark_ui_router
from app.ui.routers.judges import router as judges_ui_router
from app.ui.routers.reviews import router as reviews_ui_router
from app.ui.routes import router as ui_router

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v1.5.11: invalid judge profiles (secrets inline, unknown fields, bad values) fail fast at startup.
    load_profiles(get_settings())
    init_engine(settings)
    # v1.5.12 (D2, D7): load probe / activation state; auto-probe the environment-selected profile in the background.
    if judge_activation.RUN_ON_STARTUP:
        await judge_activation.startup(get_settings())
    yield
    await close_engine()


app = FastAPI(
    title=settings.app_name,
    version="1.5.12",
    description="Guardrail service for preventing reward hacking in agent systems.",
    lifespan=lifespan,
)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or f"trace_{uuid.uuid4().hex}"
    request.state.trace_id = trace_id
    start = perf_counter()
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    duration = perf_counter() - start
    labels = {
        "method": request.method,
        "path": request.url.path,
        "status": str(response.status_code),
    }
    HTTP_REQUESTS_TOTAL.labels(**labels).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(**labels).observe(duration)
    return response


def _safe_validation_errors(errors) -> list:
    """v1.5.12: never echo the value of a field the schema forbids (e.g. an ``api_key`` sent to a judge profile)."""
    safe = []
    for error in errors:
        item = dict(error)
        if item.get("type") == "extra_forbidden":
            item.pop("input", None)
        safe.append(item)
    return safe


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed.",
                "details": jsonable_encoder(_safe_validation_errors(exc.errors())),
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": str(exc.detail),
            }
        },
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_server_error",
                "message": "Unexpected server error.",
            }
        },
    )


app.include_router(router)
app.include_router(finops_router)
app.include_router(evaluations_router)
app.include_router(benchmark_router)
app.include_router(ui_router)
app.include_router(reviews_ui_router)
app.include_router(benchmark_ui_router)
app.include_router(judges_ui_router)
