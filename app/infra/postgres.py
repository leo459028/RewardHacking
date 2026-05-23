from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings

engine = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings):
    global engine, SessionLocal
    engine = create_async_engine(settings.postgres_dsn, echo=False, pool_pre_ping=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    return engine


async def close_engine() -> None:
    if engine is not None:
        await engine.dispose()


async def get_session() -> AsyncSession:
    if SessionLocal is None:
        raise RuntimeError("SessionLocal is not initialized")
    async with SessionLocal() as session:
        yield session
