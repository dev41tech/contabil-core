"""Gestão de sessão assíncrona com SQLAlchemy.

Também expõe SyncSessionLocal para uso em background tasks síncronos (CONCILPRO).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from src.core.config import get_settings


def _build_engine():
    settings = get_settings()
    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        echo=settings.debug,
    )


def _build_session_factory(eng):
    return async_sessionmaker(
        eng,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# Lazy: criados na primeira chamada de get_db
_engine = None
_session_factory = None


def _get_factory():
    global _engine, _session_factory
    if _session_factory is None:
        _engine = _build_engine()
        _session_factory = _build_session_factory(_engine)
    return _session_factory


class Base(DeclarativeBase):
    """Base para todos os models SQLAlchemy."""
    pass


# ── Sessão síncrona — usada pelo background task do CONCILPRO ────────────────

_sync_engine = None
_sync_session_factory = None


def get_sync_session_factory():
    """Retorna (criando se necessário) a factory de sessão síncrona via psycopg2."""
    global _sync_engine, _sync_session_factory
    if _sync_session_factory is None:
        settings = get_settings()
        # Troca o driver asyncpg → psycopg2 para uso síncrono
        sync_url = str(settings.database_url).replace("+asyncpg", "+psycopg2")
        _sync_engine = create_engine(sync_url, pool_pre_ping=True, pool_size=5)
        _sync_session_factory = sessionmaker(bind=_sync_engine, autocommit=False, autoflush=False)
    return _sync_session_factory


def SyncSessionLocal():
    """Abre uma sessão síncrona. Caller é responsável por fechar."""
    return get_sync_session_factory()()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency do FastAPI: fornece sessão de banco por request."""
    async with _get_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
