from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


class Database:
    """Owns the async SQLAlchemy engine and session factory."""

    def __init__(self, settings: Settings):
        self.settings = settings
        database_url = settings.database_url
        if database_url.startswith("postgres://"):
            database_url = "postgresql+asyncpg://" + database_url[len("postgres://") :]
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+asyncpg://" + database_url[len("postgresql://") :]
        engine_options = {
            "pool_pre_ping": True,
            "echo": settings.app_env == "development" and settings.log_level.upper() == "DEBUG",
        }
        if database_url.startswith("sqlite"):
            if settings.app_env.lower() not in {"test", "testing"}:
                raise ValueError(
                    "运行时数据库必须是 PostgreSQL；SQLite 仅允许测试环境和一次性迁移脚本"
                )
        else:
            engine_options.update(
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                pool_timeout=settings.database_pool_timeout,
            )
        self.engine: AsyncEngine = create_async_engine(database_url, **engine_options)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> float:
        import time

        started = time.perf_counter()
        async with self.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return round((time.perf_counter() - started) * 1000, 2)

    async def create_schema(self) -> None:
        from app.infrastructure.models import Base

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()


async def get_session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session
