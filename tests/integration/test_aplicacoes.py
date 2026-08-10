"""Testes de integração — CRUD de Aplicações Financeiras."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, Tenant, Usuario


def _money(value: object) -> Decimal:
    return Decimal(str(value))


# ── Helpers


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, aplicacao_id=None) -> str:
    base = f"/api/v1/empresas/{empresa_id}/aplicacoes-financeiras"
    return f"{base}/{aplicacao_id}" if aplicacao_id else base


async def _criar(client, empresa_id, csrf, **over) -> dict:
    payload = {
        "instituicao": "Banco do Brasil",
        "tipo": "cdb",
        "valor_aplicado": 10000.00,
        "data_aplicacao": "2026-01-15T00:00:00Z",
    }
    payload.update(over)
    r = await client.post(_url(empresa_id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text
    return r.json()


# ── Listar


@pytest.mark.asyncio
async def test_listar_aplicacoes_vazia(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id))
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["valor_total_aplicado"] == "0.00"
    assert body["valor_total_atual"] == "0.00"


@pytest.mark.asyncio
async def test_listar_sem_auth_rejeita(client: AsyncClient, empresa: Empresa):
    r = await client.get(_url(empresa.id))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_listar_soma_valor_aplicado_e_atual(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    await _criar(client, empresa.id, csrf, valor_aplicado=10000.00)
    await _criar(client, empresa.id, csrf, valor_aplicado=5000.00, valor_atual=5300.00)

    r = await client.get(_url(empresa.id))
    body = r.json()
    assert body["total"] == 2
    # sem valor_atual informado, usa o valor_aplicado como corrente
    assert body["valor_total_aplicado"] == "15000.00"
    assert body["valor_total_atual"] == "15300.00"


# ── Criar


@pytest.mark.asyncio
async def test_criar_aplicacao_sucesso(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={
            "instituicao": "Nubank",
            "tipo": "cdb",
            "descricao": "CDB 110% CDI",
            "valor_aplicado": 8000.00,
            "data_aplicacao": "2026-02-01T00:00:00Z",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["instituicao"] == "Nubank"
    assert body["tipo"] == "cdb"
    assert _money(body["valor_aplicado"]) == Decimal("8000.00")
    assert body["valor_atual"] is None
    assert body["rendimento"] is None
    assert body["ativa"] is True
    assert body["empresa_id"] == str(empresa.id)


@pytest.mark.asyncio
async def test_criar_aplicacao_com_valor_atual_calcula_rendimento(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    body = await _criar(client, empresa.id, csrf, valor_aplicado=10000.00, valor_atual=10450.00)

    assert _money(body["rendimento"]) == Decimal("450.00")
    assert body["data_atualizacao_valor"] is not None


@pytest.mark.asyncio
async def test_criar_aplicacao_tipo_invalido_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={
            "instituicao": "Banco X",
            "tipo": "bitcoin",
            "valor_aplicado": 100.00,
            "data_aplicacao": "2026-01-01T00:00:00Z",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_aplicacao_valor_negativo_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={
            "instituicao": "Banco X",
            "tipo": "poupanca",
            "valor_aplicado": -100.00,
            "data_aplicacao": "2026-01-01T00:00:00Z",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_aplicacao_com_agencia_de_outra_empresa_rejeita(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    from src.db.models import AgenciaBancaria, Empresa as EmpresaModel

    outra_empresa = EmpresaModel(
        tenant_id=tenant.id, razao_social="Outra LTDA",
        cnpj="11.222.333/0001-44", regime_tributario="lucro_real",
    )
    db.add(outra_empresa)
    await db.flush()
    agencia_outra = AgenciaBancaria(
        empresa_id=outra_empresa.id, banco_sigla="BB", agencia="0001", numero="99999"
    )
    db.add(agencia_outra)
    await db.flush()

    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={
            "instituicao": "Banco X",
            "tipo": "poupanca",
            "valor_aplicado": 100.00,
            "data_aplicacao": "2026-01-01T00:00:00Z",
            "agencia_id": str(agencia_outra.id),
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_aplicacao_sem_csrf_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={
            "instituicao": "Banco X",
            "tipo": "cdb",
            "valor_aplicado": 100.00,
            "data_aplicacao": "2026-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 403


# ── Obter


@pytest.mark.asyncio
async def test_obter_aplicacao_existente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = await _criar(client, empresa.id, csrf)

    r = await client.get(_url(empresa.id, criada["id"]))
    assert r.status_code == 200
    assert r.json()["id"] == criada["id"]


@pytest.mark.asyncio
async def test_obter_aplicacao_inexistente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id, uuid.uuid4()))
    assert r.status_code == 404


# ── Atualizar


@pytest.mark.asyncio
async def test_atualizar_valor_atual_recalcula_rendimento(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = await _criar(client, empresa.id, csrf, valor_aplicado=10000.00)

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"valor_atual": 10800.00},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert _money(r.json()["valor_atual"]) == Decimal("10800.00")
    assert _money(r.json()["rendimento"]) == Decimal("800.00")
    assert r.json()["data_atualizacao_valor"] is not None


@pytest.mark.asyncio
async def test_encerrar_aplicacao_via_update(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Encerrar (resgatar) usa PATCH ativa=false — não deleta o registro."""
    csrf = await _login(client, tenant, usuario)
    criada = await _criar(client, empresa.id, csrf)

    r = await client.patch(
        _url(empresa.id, criada["id"]),
        json={"ativa": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["ativa"] is False

    lista_todas = await client.get(_url(empresa.id))
    lista_ativas = await client.get(_url(empresa.id) + "?apenas_ativas=true")
    assert criada["id"] in [a["id"] for a in lista_todas.json()["items"]]
    assert criada["id"] not in [a["id"] for a in lista_ativas.json()["items"]]


# ── Remover


@pytest.mark.asyncio
async def test_remover_aplicacao(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criada = await _criar(client, empresa.id, csrf)

    r = await client.delete(_url(empresa.id, criada["id"]), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204

    lista = await client.get(_url(empresa.id))
    assert criada["id"] not in [a["id"] for a in lista.json()["items"]]


# ── Isolamento de empresa


@pytest.mark.asyncio
async def test_aplicacao_nao_visivel_em_outra_empresa(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    from src.db.models import Empresa as EmpresaModel

    empresa2 = EmpresaModel(
        tenant_id=tenant.id, razao_social="Outra Empresa LTDA",
        cnpj="99.888.777/0001-00", regime_tributario="lucro_real",
    )
    db.add(empresa2)
    await db.flush()

    csrf = await _login(client, tenant, usuario)
    await _criar(client, empresa.id, csrf)

    r = await client.get(_url(empresa2.id))
    assert r.status_code == 200
    assert r.json()["items"] == []
