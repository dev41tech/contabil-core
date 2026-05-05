"""Testes de integração do CRUD de empresas."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from src.db.models import Empresa, Tenant, Usuario


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    """Helper: faz login e retorna csrf_token."""
    r = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": usuario.email,
            "senha": "senha_segura_123",
        },
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


@pytest.mark.asyncio
async def test_listar_empresas_sem_autenticacao(client: AsyncClient):
    r = await client.get("/api/v1/empresas")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_listar_empresas_autenticado(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get("/api/v1/empresas")
    assert r.status_code == 200

    body = r.json()
    assert body["total"] >= 1
    assert any(e["cnpj"] == empresa.cnpj for e in body["items"])


@pytest.mark.asyncio
async def test_criar_empresa(client: AsyncClient, tenant: Tenant, usuario: Usuario):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Nova Empresa Teste LTDA",
            "cnpj": "98.765.432/0001-10",
            "regime_tributario": "lucro_presumido",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["razao_social"] == "Nova Empresa Teste LTDA"
    assert body["cnpj"] == "98.765.432/0001-10"
    assert "id" in body


@pytest.mark.asyncio
async def test_criar_empresa_cnpj_duplicado(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Outra Empresa LTDA",
            "cnpj": empresa.cnpj,  # CNPJ já existente
            "regime_tributario": "simples_nacional",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "CONFLICT"


@pytest.mark.asyncio
async def test_obter_empresa_existente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(f"/api/v1/empresas/{empresa.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(empresa.id)


@pytest.mark.asyncio
async def test_obter_empresa_inexistente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario
):
    import uuid
    await _login(client, tenant, usuario)
    r = await client.get(f"/api/v1/empresas/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"] == "EMPRESA_NAO_ENCONTRADA"


@pytest.mark.asyncio
async def test_atualizar_empresa(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.patch(
        f"/api/v1/empresas/{empresa.id}",
        json={"razao_social": "DECATEC LTDA ATUALIZADO"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["razao_social"] == "DECATEC LTDA ATUALIZADO"


@pytest.mark.asyncio
async def test_criar_empresa_sem_csrf_falha(
    client: AsyncClient, tenant: Tenant, usuario: Usuario
):
    await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Empresa Sem CSRF",
            "cnpj": "11.111.111/0001-11",
            "regime_tributario": "lucro_real",
        },
        # sem X-CSRF-Token
    )
    assert r.status_code == 403
