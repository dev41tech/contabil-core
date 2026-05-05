"""Testes de integração — Importação de Extrato OFX."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient

from src.db.models import Empresa, Tenant, Usuario

# OFX 1.x mínimo para testes
_OFX_VALIDO = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>1001
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>BRL
<BANKTRANLIST>
<DTSTART>20240101
<DTEND>20240131
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240115
<TRNAMT>1500.00
<FITID>TX001
<MEMO>TED RECEBIDA EMPRESA XYZ
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240116
<TRNAMT>-250.00
<FITID>TX002
<MEMO>BOLETO FORNECEDOR ABC
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


async def _criar_agencia(client, empresa, csrf) -> dict:
    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/agencias",
        json={"banco_sigla": "BB", "agencia": "0001", "numero": "11111"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    return r.json()


async def _importar(client, empresa, agencia_id, csrf, conteudo=None) -> dict:
    conteudo = conteudo or _OFX_VALIDO
    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia_id}",
        files={"arquivo": ("extrato.ofx", io.BytesIO(conteudo.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    return r


# ── Importação


@pytest.mark.asyncio
async def test_importar_ofx_sucesso(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    r = await _importar(client, empresa, agencia["id"], csrf)
    assert r.status_code == 201
    body = r.json()
    assert body["importadas"] == 2
    assert body["duplicadas"] == 0
    assert len(body["transacoes"]) == 2


@pytest.mark.asyncio
async def test_importar_ofx_deduplicacao(client, tenant, usuario, empresa):
    """Segunda importação do mesmo arquivo deve registrar 2 duplicadas."""
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    r1 = await _importar(client, empresa, agencia["id"], csrf)
    assert r1.status_code == 201
    assert r1.json()["importadas"] == 2

    r2 = await _importar(client, empresa, agencia["id"], csrf)
    assert r2.status_code == 201
    body2 = r2.json()
    assert body2["importadas"] == 0
    assert body2["duplicadas"] == 2


@pytest.mark.asyncio
async def test_importar_ofx_invalido_rejeita(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    conteudo_invalido = "isso nao e um arquivo ofx valido !!!"
    r = await _importar(client, empresa, agencia["id"], csrf, conteudo=conteudo_invalido)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_importar_sem_csrf_rejeita(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("extrato.ofx", io.BytesIO(_OFX_VALIDO.encode()), "application/octet-stream")},
    )
    assert r.status_code == 403


# ── Listar


@pytest.mark.asyncio
async def test_listar_transacoes(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _importar(client, empresa, agencia["id"], csrf)

    r = await client.get(f"/api/v1/empresas/{empresa.id}/extrato")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 2
    assert len(body["items"]) >= 2


@pytest.mark.asyncio
async def test_listar_filtro_status_pendente(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _importar(client, empresa, agencia["id"], csrf)

    r = await client.get(f"/api/v1/empresas/{empresa.id}/extrato?status=pendente")
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["status"] == "pendente"


# ── Obter


@pytest.mark.asyncio
async def test_obter_transacao_existente(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    importado = await _importar(client, empresa, agencia["id"], csrf)
    transacao_id = importado.json()["transacoes"][0]["id"]

    r = await client.get(f"/api/v1/empresas/{empresa.id}/extrato/{transacao_id}")
    assert r.status_code == 200
    assert r.json()["id"] == transacao_id


@pytest.mark.asyncio
async def test_obter_transacao_inexistente(client, tenant, usuario, empresa):
    import uuid
    await _login(client, tenant, usuario)
    r = await client.get(f"/api/v1/empresas/{empresa.id}/extrato/{uuid.uuid4()}")
    assert r.status_code == 404
