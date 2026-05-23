from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

from app.core.config import Settings
from app.models.base import Base

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


async def check_connection() -> None:
    if SessionLocal is None:
        raise RuntimeError("SessionLocal is not initialized")
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))


async def create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    if SessionLocal is None:
        raise RuntimeError("SessionLocal is not initialized")
    async with SessionLocal() as session:
        yield session
