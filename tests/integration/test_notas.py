"""Testes de integração — Notas Fiscais (NF-e / NFS-e)."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, Tenant, Usuario

_OFX_SIMPLES = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240201
<TRNAMT>-800.00
<FITID>NF001
<MEMO>PAGTO FORNECEDOR NF
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


async def _login(client, tenant, usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


async def _criar_agencia(client, empresa, csrf) -> dict:
    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/agencias",
        json={"banco_sigla": "ITAU", "agencia": "0002", "numero": "22222"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    return r.json()


async def _criar_transacao(client, empresa, agencia_id, csrf) -> str:
    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia_id}",
        files={"arquivo": ("extrato.ofx", io.BytesIO(_OFX_SIMPLES.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    return r.json()["transacoes"][0]["id"]


def _url(empresa_id, nota_id=None, extra="") -> str:
    base = f"/api/v1/empresas/{empresa_id}/notas"
    if nota_id:
        return f"{base}/{nota_id}{extra}"
    return base


_NOTA_PAYLOAD = {
    "tipo": "nfe",
    "numero": "000001",
    "cnpj_emitente": "12345678000190",
    "valor": 800.00,
    "data_emissao": "2024-02-01T00:00:00Z",
}


# ── Criar


@pytest.mark.asyncio
async def test_criar_nota_sucesso(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(_url(empresa.id), json=_NOTA_PAYLOAD, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201
    body = r.json()
    assert body["tipo"] == "nfe"
    assert body["status"] == "pendente"
    assert body["transacao_id"] is None


@pytest.mark.asyncio
async def test_criar_nota_tipo_invalido(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    payload = {**_NOTA_PAYLOAD, "tipo": "boleto", "numero": "000099"}
    r = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_nota_chave_duplicada_rejeita(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    payload = {**_NOTA_PAYLOAD, "numero": "DUP001", "chave_acesso": "12345678901234567890123456789012345678901234"}
    r1 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 201
    r2 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 409


# ── Listar


@pytest.mark.asyncio
async def test_listar_notas(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "LST001"}, headers={"X-CSRF-Token": csrf})
    r = await client.get(_url(empresa.id))
    assert r.status_code == 200
    assert r.json()["total"] >= 1


@pytest.mark.asyncio
async def test_listar_filtro_status(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "FLT001"}, headers={"X-CSRF-Token": csrf})
    r = await client.get(_url(empresa.id) + "?status=pendente")
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["status"] == "pendente"


# ── Associar / Desassociar


@pytest.mark.asyncio
async def test_associar_e_desassociar_transacao(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    transacao_id = await _criar_transacao(client, empresa, agencia["id"], csrf)

    nota = (
        await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "ASS001"}, headers={"X-CSRF-Token": csrf})
    ).json()

    # Associar
    r = await client.post(
        _url(empresa.id, nota["id"], "/associar"),
        json={"transacao_id": transacao_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "associada"
    assert r.json()["transacao_id"] == transacao_id

    # Desassociar
    r2 = await client.delete(_url(empresa.id, nota["id"], "/associar"), headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 200
    assert r2.json()["status"] == "pendente"
    assert r2.json()["transacao_id"] is None


@pytest.mark.asyncio
async def test_associar_ja_associada_rejeita(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    transacao_id = await _criar_transacao(client, empresa, agencia["id"], csrf)

    nota = (
        await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "ASS002"}, headers={"X-CSRF-Token": csrf})
    ).json()

    await client.post(
        _url(empresa.id, nota["id"], "/associar"),
        json={"transacao_id": transacao_id},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.post(
        _url(empresa.id, nota["id"], "/associar"),
        json={"transacao_id": transacao_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


# ── Cancelar


@pytest.mark.asyncio
async def test_cancelar_nota(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    nota = (
        await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "CAN001"}, headers={"X-CSRF-Token": csrf})
    ).json()

    r = await client.post(_url(empresa.id, nota["id"], "/cancelar"), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["status"] == "cancelada"


@pytest.mark.asyncio
async def test_cancelar_impede_associacao(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    transacao_id = await _criar_transacao(client, empresa, agencia["id"], csrf)

    nota = (
        await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "CAN002"}, headers={"X-CSRF-Token": csrf})
    ).json()
    await client.post(_url(empresa.id, nota["id"], "/cancelar"), headers={"X-CSRF-Token": csrf})

    r = await client.post(
        _url(empresa.id, nota["id"], "/associar"),
        json={"transacao_id": transacao_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422
