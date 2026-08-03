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
    """A aplicação sobe, responde no health check e confirma que enxerga o banco."""
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "version" in body
    assert "commit" in body


@pytest.mark.asyncio
async def test_health_check_503_quando_banco_fora(client: AsyncClient, monkeypatch):
    """Banco indisponível precisa virar 503 — não um 200 que engana o painel."""
    from src.api import app as app_module

    async def _falha(_db):
        return "error"

    monkeypatch.setattr(app_module, "_checar_banco", _falha)

    r = await client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"


@pytest.mark.asyncio
async def test_health_live_nao_depende_do_banco(client: AsyncClient, monkeypatch):
    """Liveness responde 200 mesmo com o banco fora — é o alvo do HEALTHCHECK do Docker."""
    from src.api import app as app_module

    async def _falha(_db):
        return "error"

    monkeypatch.setattr(app_module, "_checar_banco", _falha)

    r = await client.get("/api/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_checar_banco_devolve_error_sem_derrubar(db):
    """_checar_banco engole a falha e devolve 'error' em vez de propagar."""
    from src.api.app import _checar_banco

    class _SessaoQuebrada:
        async def execute(self, *_a, **_kw):
            raise RuntimeError("connection refused")

        async def rollback(self):
            pass

    assert await _checar_banco(_SessaoQuebrada()) == "error"
    assert await _checar_banco(db) == "ok"


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
