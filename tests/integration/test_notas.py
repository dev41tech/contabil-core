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
    "cnpj_emitente": "12345678000195",
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


@pytest.mark.asyncio
async def test_mesma_chave_pode_existir_em_empresas_diferentes(
    client, db, tenant, usuario, empresa
):
    outra = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA EMPRESA LTDA",
        cnpj="98.765.432/0001-10",
        regime_tributario="lucro_real",
    )
    db.add(outra)
    await db.flush()
    csrf = await _login(client, tenant, usuario)
    payload = {**_NOTA_PAYLOAD, "numero": "ESCOPO001", "chave_acesso": "9" * 44}

    r1 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    r2 = await client.post(_url(outra.id), json=payload, headers={"X-CSRF-Token": csrf})

    assert r1.status_code == 201
    assert r2.status_code == 201


@pytest.mark.asyncio
async def test_nfse_sem_chave_e_deduplicada_por_identidade_composta(
    client, tenant, usuario, empresa
):
    csrf = await _login(client, tenant, usuario)
    payload = {**_NOTA_PAYLOAD, "tipo": "nfse", "numero": "NFSE-42"}

    r1 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    r2 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})

    assert r1.status_code == 201
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


@pytest.mark.asyncio
async def test_listar_busca_por_numero_parcial(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "BUSCA-12345"}, headers={"X-CSRF-Token": csrf})
    await client.post(_url(empresa.id), json={**_NOTA_PAYLOAD, "numero": "OUTRO-99999"}, headers={"X-CSRF-Token": csrf})

    r = await client.get(_url(empresa.id) + "?numero=12345")
    assert r.status_code == 200
    numeros = [item["numero"] for item in r.json()["items"]]
    assert "BUSCA-12345" in numeros
    assert "OUTRO-99999" not in numeros


@pytest.mark.asyncio
async def test_listar_busca_por_chave_acesso_parcial(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    chave = "".join(str(i % 10) for i in range(44))  # 44 dígitos determinísticos
    await client.post(
        _url(empresa.id),
        json={**_NOTA_PAYLOAD, "numero": "CHV001", "chave_acesso": chave},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.get(_url(empresa.id) + f"?chave_acesso={chave[10:20]}")
    assert r.status_code == 200
    assert any(item["chave_acesso"] == chave for item in r.json()["items"])


@pytest.mark.asyncio
async def test_listar_busca_por_cnpj_completo_e_parcial(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await client.post(
        _url(empresa.id),
        json={**_NOTA_PAYLOAD, "numero": "CNPJ001", "cnpj_emitente": "12345678000195"},
        headers={"X-CSRF-Token": csrf},
    )

    exato = await client.get(_url(empresa.id) + "?cnpj=12345678000195")
    assert exato.status_code == 200
    assert any(item["numero"] == "CNPJ001" for item in exato.json()["items"])

    parcial = await client.get(_url(empresa.id) + "?cnpj=12.345.678")
    assert parcial.status_code == 200
    assert any(item["numero"] == "CNPJ001" for item in parcial.json()["items"])


@pytest.mark.asyncio
async def test_listar_busca_por_emitente_parcial_case_insensitive(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await client.post(
        _url(empresa.id),
        json={**_NOTA_PAYLOAD, "numero": "EMIT001", "nome_emitente": "Distribuidora Sao Jose LTDA"},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.get(_url(empresa.id) + "?emitente=sao jose")
    assert r.status_code == 200
    assert any(item["numero"] == "EMIT001" for item in r.json()["items"])


@pytest.mark.asyncio
async def test_listar_filtro_periodo_data_emissao(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await client.post(
        _url(empresa.id),
        json={**_NOTA_PAYLOAD, "numero": "PER001", "data_emissao": "2024-05-15T00:00:00Z"},
        headers={"X-CSRF-Token": csrf},
    )
    await client.post(
        _url(empresa.id),
        json={**_NOTA_PAYLOAD, "numero": "PER002", "data_emissao": "2024-08-15T00:00:00Z"},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.get(
        _url(empresa.id) + "?data_de=2024-05-01T00:00:00Z&data_ate=2024-05-31T23:59:59Z"
    )
    assert r.status_code == 200
    numeros = [item["numero"] for item in r.json()["items"]]
    assert "PER001" in numeros
    assert "PER002" not in numeros


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
