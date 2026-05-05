"""Smoke tests — validam que a aplicação sobe e responde.

Esses testes rodam contra a aplicação em memória (sem banco real).
Para smoke tests em staging/prod, use variável SMOKE_BASE_URL.
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient

SMOKE_BASE_URL = os.getenv("SMOKE_BASE_URL", "")


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    """A aplicação sobe e responde no health check."""
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


@pytest.mark.asyncio
async def test_docs_disponiveis_em_dev(client: AsyncClient):
    """Swagger disponível em desenvolvimento."""
    r = await client.get("/api/docs")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_openapi_schema_disponivel(client: AsyncClient):
    """Schema OpenAPI gerado."""
    r = await client.get("/api/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "Contabil Core API"
    assert "/api/v1/auth/login" in schema["paths"]
    assert "/api/v1/empresas" in schema["paths"]


@pytest.mark.asyncio
async def test_endpoint_inexistente_retorna_404(client: AsyncClient):
    r = await client.get("/api/v1/nao-existe")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_login_sem_body_retorna_422(client: AsyncClient):
    r = await client.post("/api/v1/auth/login", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_trace_id_presente_na_resposta(client: AsyncClient):
    """Todo response deve ter X-Trace-ID."""
    r = await client.get("/api/health")
    assert "x-trace-id" in r.headers


@pytest.mark.asyncio
async def test_erro_tipado_tem_estrutura_correta(client: AsyncClient):
    """Erros retornam JSON com campos 'error' e 'message'."""
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
    body = r.json()
    assert "error" in body
    assert "message" in body
