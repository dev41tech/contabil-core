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


# ── Exportar extrato bancário


_OFX_TRES_TX = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240501
<TRNAMT>1200.00
<FITID>EXT_TX1
<MEMO>RECEBIMENTO CLIENTE ALFA
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240610
<TRNAMT>-350.00
<FITID>EXT_TX2
<MEMO>PAGAMENTO ENERGIA
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240715
<TRNAMT>-90.00
<FITID>EXT_TX3
<MEMO>TARIFA BANCARIA
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


async def _setup_extrato(client, empresa, csrf) -> str:
    """Importa 3 transações (mai, jun, jul de 2024). Retorna o agencia_id."""
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "ITAU", "agencia": "7285", "numero": "12287"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("e.ofx", io.BytesIO(_OFX_TRES_TX.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    return agencia["id"]


def _linhas_csv(resposta) -> list[str]:
    return resposta.content.decode("utf-8-sig").strip().split("\n")


@pytest.mark.asyncio
async def test_exportar_extrato_traz_as_transacoes(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_extrato(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv", "tipo": "extrato"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.headers.get("X-Total-Registros") == "3"

    linhas = _linhas_csv(r)
    assert linhas[0].strip() == "data,agencia,historico,dc,valor,status"
    assert len(linhas) == 4  # cabeçalho + 3
    assert "RECEBIMENTO CLIENTE ALFA" in r.text
    assert "ITAU 7285 12287" in r.text, "a agência precisa vir resolvida, não como UUID"


@pytest.mark.asyncio
async def test_exportar_extrato_respeita_o_periodo(client, tenant, usuario, empresa):
    """O arquivo tem que bater com o que a tela mostra — inclusive nos filtros."""
    csrf = await _login(client, tenant, usuario)
    await _setup_extrato(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id),
        json={
            "formato": "csv",
            "tipo": "extrato",
            "data_de": "2024-06-01T00:00:00Z",
            "data_ate": "2024-06-30T23:59:59Z",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.headers.get("X-Total-Registros") == "1"
    assert "PAGAMENTO ENERGIA" in r.text
    assert "RECEBIMENTO CLIENTE ALFA" not in r.text


@pytest.mark.asyncio
async def test_exportar_extrato_respeita_o_filtro_de_agencia(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia_id = await _setup_extrato(client, empresa, csrf)

    outra = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "BB", "agencia": "0001", "numero": "99999"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv", "tipo": "extrato", "agencia_id": outra["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.headers.get("X-Total-Registros") == "0", "agência sem movimento não traz linha"

    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv", "tipo": "extrato", "agencia_id": agencia_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.headers.get("X-Total-Registros") == "3"


@pytest.mark.asyncio
async def test_exportar_extrato_respeita_o_filtro_de_status(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_extrato(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv", "tipo": "extrato", "status": "pendente"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.headers.get("X-Total-Registros") == "3"

    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv", "tipo": "extrato", "status": "processada"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.headers.get("X-Total-Registros") == "0"


@pytest.mark.asyncio
async def test_exportar_extrato_em_xlsx(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_extrato(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id),
        json={"formato": "xlsx", "tipo": "extrato"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"


@pytest.mark.asyncio
async def test_exportar_extrato_ordena_por_data(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_extrato(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id),
        json={"formato": "csv", "tipo": "extrato"},
        headers={"X-CSRF-Token": csrf},
    )
    datas = [l.split(",")[0] for l in _linhas_csv(r)[1:]]
    assert datas == ["01/05/2024", "10/06/2024", "15/07/2024"]
