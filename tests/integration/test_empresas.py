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
async def test_listar_cnpj_invalidos(
    client: AsyncClient, db, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Empresas com CNPJ placeholder da migração precisam aparecer numa lista.

    Enquanto ficam invisíveis, a exportação de notas delas volta vazia e ninguém
    descobre por quê.
    """
    placeholder = Empresa(
        tenant_id=tenant.id,
        razao_social="AJAF LOGISTICA LTDA",
        cnpj="01.003.007/0001-01",  # padrão gerado pelo scripts/import_mrcont.py
        regime_tributario="lucro_presumido",
    )
    db.add(placeholder)
    await db.flush()

    await _login(client, tenant, usuario)
    r = await client.get("/api/v1/empresas/cnpj-invalidos")
    assert r.status_code == 200

    body = r.json()
    cnpjs = [e["cnpj"] for e in body["items"]]
    assert "01.003.007/0001-01" in cnpjs
    assert empresa.cnpj not in cnpjs, "empresa com CNPJ válido não deveria estar na lista"
    assert body["total"] == len(body["items"])
    assert body["total_empresas"] >= body["total"]


@pytest.mark.asyncio
async def test_listar_cnpj_invalidos_exige_autenticacao(client: AsyncClient):
    r = await client.get("/api/v1/empresas/cnpj-invalidos")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_criar_empresa_com_cnpj_invalido_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario
):
    """O cadastro passou a barrar na entrada o que gerou as 72 empresas quebradas."""
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Empresa CNPJ Falso LTDA",
            "cnpj": "01.003.007/0001-01",
            "regime_tributario": "lucro_presumido",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_empresa(client: AsyncClient, tenant: Tenant, usuario: Usuario):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Nova Empresa Teste LTDA",
            "cnpj": "98.765.432/0001-98",
            "regime_tributario": "lucro_presumido",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["razao_social"] == "Nova Empresa Teste LTDA"
    assert body["cnpj"] == "98.765.432/0001-98"
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
            "cnpj": "11.111.111/0001-91",
            "regime_tributario": "lucro_real",
        },
        # sem X-CSRF-Token
    )
    assert r.status_code == 403
