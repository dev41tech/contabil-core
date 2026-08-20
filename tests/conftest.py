"""Fixtures compartilhadas por todos os testes.

Usa banco SQLite em memória para testes unitários/integração sem Docker.
Para testes de integração real com PostgreSQL, use a variável TEST_DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Variáveis de ambiente para o Settings antes de qualquer import da app
# Em testes unitários/integração sem PostgreSQL, usamos SQLite mas o Settings
# precisa de uma URL PostgreSQL válida para passar na validação do Pydantic.
# A URL de banco real é injetada via fixture engine abaixo.
os.environ.setdefault("SECRET_KEY", "test-secret-key-minimo-32-caracteres-aqui")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("COOKIE_SECURE", "false")   # http://test não é HTTPS
os.environ.setdefault("ENVIRONMENT", "development")

from src.api.app import create_app  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.core.security import hash_password  # noqa: E402
from src.db.models import Base, Empresa, Tenant, Usuario  # noqa: E402
from src.db.session import get_db  # noqa: E402
from src.domain.jobs import JobRuntime  # noqa: E402

# Limpa o cache do Settings para aplicar as variáveis de ambiente acima
get_settings.cache_clear()

# ── Banco de teste real (SQLite em memória para testes sem Docker)
TEST_DB_URL = os.getenv("TEST_DATABASE_URL_REAL", "sqlite+aiosqlite:///:memory:")


@pytest_asyncio.fixture(scope="session")
async def engine():
    e = create_async_engine(TEST_DB_URL, echo=False)
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await e.dispose()


@pytest_asyncio.fixture
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
        await session.rollback()  # limpa entre testes


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def job_session_scope():
        # BackgroundTasks roda inline no transporte ASGI. Usar a sessão isolada
        # do teste preserva o rollback por caso; produção sempre abre outra.
        yield db

    app.state.job_runtime = JobRuntime(session_scope=job_session_scope, commit=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Factories de dados


@pytest_asyncio.fixture
async def tenant(db: AsyncSession) -> Tenant:
    t = Tenant(nome="41 Contábil", cnpj="41.000.000/0001-79")
    db.add(t)
    await db.flush()
    return t


@pytest_asyncio.fixture
async def usuario(db: AsyncSession, tenant: Tenant) -> Usuario:
    u = Usuario(
        tenant_id=tenant.id,
        email="contador@41contabil.com.br",
        nome="Luiz Contador",
        senha_hash=hash_password("senha_segura_123"),
        role="admin",
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def empresa(db: AsyncSession, tenant: Tenant) -> Empresa:
    e = Empresa(
        tenant_id=tenant.id,
        razao_social="DECATEC LTDA",
        cnpj="12.345.678/0001-95",
        regime_tributario="simples_nacional",
    )
    db.add(e)
    await db.flush()
    return e
