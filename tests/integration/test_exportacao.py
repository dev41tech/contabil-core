"""Testes de integração — Exportação para ERP."""

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
<DTPOSTED>20240501
<TRNAMT>1200.00
<FITID>EXP_TX1
<MEMO>SERVICO PRESTADO CLIENTE X
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


async def _setup_registros(client, db, empresa, csrf):
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "SAFRA", "agencia": "0005", "numero": "55555"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    conta = PlanoConta(empresa_id=empresa.id, codigo="4.1.1", descricao="Serviços Prestados", tipo="receita")
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
            "descricao": "Serviço Prestado",
            "historico": "SERVICO",
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


def _url(empresa_id) -> str:
    return f"/api/v1/empresas/{empresa_id}/exportacao/gerar"


# ── Exportar CSV


@pytest.mark.asyncio
async def test_exportar_csv(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_registros(client, db, empresa, csrf)

    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "Content-Disposition" in r.headers
    assert r.headers["Content-Disposition"].endswith(".csv\"")

    # Verifica que tem dados (cabeçalho + pelo menos 1 linha)
    linhas = r.content.decode("utf-8-sig").strip().split("\n")
    assert len(linhas) >= 2  # header + data
    assert "historico" in linhas[0]


@pytest.mark.asyncio
async def test_exportar_xlsx(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_registros(client, db, empresa, csrf)

    r = await client.post(
        _url(empresa.id),
        json={"formato": "xlsx"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    # Verifica magic bytes do XLSX (PK zip)
    assert r.content[:2] == b"PK"


@pytest.mark.asyncio
async def test_exportar_formato_invalido(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"formato": "pdf"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_exportar_sem_csrf_rejeita(client, tenant, usuario, empresa):
    await _login(client, tenant, usuario)
    r = await client.post(_url(empresa.id), json={"formato": "csv"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_exportar_sem_registros_retorna_vazio(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    # Só cabeçalho, sem linhas de dados
    linhas = r.content.decode("utf-8-sig").strip().split("\n")
    assert len(linhas) == 1  # apenas cabeçalho
    assert r.headers.get("X-Total-Registros") == "0"
