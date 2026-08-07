"""Testes de integração — Registros Contábeis."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, PlanoConta, Tenant, Usuario

_OFX_MINI = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240401
<TRNAMT>3000.00
<FITID>CTB_TX1
<MEMO>VENDA PRODUTO A
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


async def _login(client, tenant, usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


async def _setup_e_processar(client, db, empresa, csrf):
    """Cria infraestrutura mínima, importa OFX, cria regra e processa NEO."""
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "CEF", "agencia": "0004", "numero": "44444"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    conta = PlanoConta(empresa_id=empresa.id, codigo="3.1.1", descricao="Vendas", tipo="receita")
    db.add(conta)
    await db.flush()

    await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("e.ofx", io.BytesIO(_OFX_MINI.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )

    await client.post(
        f"/api/v1/empresas/{empresa.id}/regras",
        json={
            "conta_id": str(conta.id),
            "agencia_id": agencia["id"],
            "descricao": "Venda Produto",
            "historico": "VENDA PRODUTO A",
            "dc": "C",
            "tipo": "automatica",
        },
        headers={"X-CSRF-Token": csrf},
    )

    await client.post(
        f"/api/v1/empresas/{empresa.id}/neo/processar",
        json={},
        headers={"X-CSRF-Token": csrf},
    )


# ── Listar


@pytest.mark.asyncio
async def test_listar_registros_contabeis(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_e_processar(client, db, empresa, csrf)

    r = await client.get(f"/api/v1/empresas/{empresa.id}/contabil")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {item["dc"] for item in body["items"]} == {"D", "C"}
    assert all(item["valor"] == 3000.0 for item in body["items"])
    assert len({item["lancamento_id"] for item in body["items"]}) == 1


@pytest.mark.asyncio
async def test_listar_registros_filtro_dc(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_e_processar(client, db, empresa, csrf)

    r = await client.get(f"/api/v1/empresas/{empresa.id}/contabil?dc=C")
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["dc"] == "C"

    r2 = await client.get(f"/api/v1/empresas/{empresa.id}/contabil?dc=D")
    assert r2.status_code == 200
    # A contrapartida bancária da receita é um débito.
    assert r2.json()["total"] == 1


@pytest.mark.asyncio
async def test_listar_registros_data_ate_inclui_dia_inteiro(
    client, db, tenant, usuario, empresa
):
    csrf = await _login(client, tenant, usuario)
    await _setup_e_processar(client, db, empresa, csrf)

    r = await client.get(
        f"/api/v1/empresas/{empresa.id}/contabil", params={"data_ate": "2024-04-01"}
    )

    assert r.status_code == 200
    assert r.json()["total"] == 2


@pytest.mark.asyncio
async def test_listar_registros_data_invalida_retorna_422(
    client, tenant, usuario, empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(
        f"/api/v1/empresas/{empresa.id}/contabil", params={"data_ate": "nao-e-data"}
    )
    assert r.status_code == 422


# ── Obter


@pytest.mark.asyncio
async def test_obter_registro_existente(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_e_processar(client, db, empresa, csrf)

    lista = await client.get(f"/api/v1/empresas/{empresa.id}/contabil")
    registro_id = lista.json()["items"][0]["id"]

    r = await client.get(f"/api/v1/empresas/{empresa.id}/contabil/{registro_id}")
    assert r.status_code == 200
    assert r.json()["id"] == registro_id


@pytest.mark.asyncio
async def test_obter_registro_inexistente(client, tenant, usuario, empresa):
    import uuid
    await _login(client, tenant, usuario)
    r = await client.get(f"/api/v1/empresas/{empresa.id}/contabil/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_sem_auth_rejeita(client, empresa):
    r = await client.get(f"/api/v1/empresas/{empresa.id}/contabil")
    assert r.status_code == 401
