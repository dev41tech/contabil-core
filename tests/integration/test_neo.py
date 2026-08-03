"""Testes de integração — NEO (motor de matching automático)."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgenciaBancaria, Empresa, PlanoConta, Tenant, Usuario

_OFX_TED = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240301
<TRNAMT>5000.00
<FITID>NEO_TX_C
<MEMO>TED RECEBIDA CLIENTE ALFA
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240301
<TRNAMT>-200.00
<FITID>NEO_TX_D
<MEMO>BOLETO ENERGIA ELETRICA
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240302
<TRNAMT>100.00
<FITID>NEO_TX_SEM_REGRA
<MEMO>DEPOSITO AVULSO DESCONHECIDO
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


async def _setup_base(client, db, empresa, csrf):
    """Cria agência + conta + importa extrato. Retorna (agencia_id, conta_id)."""
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "BRADESCO", "agencia": "0003", "numero": "33333"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    conta = PlanoConta(empresa_id=empresa.id, codigo="2.1.1", descricao="Receitas TED", tipo="receita")
    db.add(conta)
    await db.flush()

    # Importa extrato
    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("e.ofx", io.BytesIO(_OFX_TED.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201

    return agencia["id"], str(conta.id)


async def _criar_regra(client, empresa, agencia_id, conta_id, historico, dc, csrf) -> dict:
    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/regras",
        json={
            "conta_id": conta_id,
            "agencia_id": agencia_id,
            "descricao": f"Regra {historico}",
            "historico": historico,
            "dc": dc,
            "tipo": "automatica",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    return r.json()


def _processar_url(empresa_id) -> str:
    return f"/api/v1/empresas/{empresa_id}/neo/processar"


# ── NEO processar


@pytest.mark.asyncio
async def test_neo_processa_com_match(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)

    # Regra exata para TED crédito
    await _criar_regra(client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf)

    r = await client.post(
        _processar_url(empresa.id),
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["associadas"] >= 1
    assert "total_pendentes" in body
    assert "sem_regra" in body


@pytest.mark.asyncio
async def test_neo_processa_substring(client, db, tenant, usuario, empresa):
    """Regra com substring do histórico deve ser encontrada."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)

    # Regra substring — "BOLETO" está contido em "BOLETO ENERGIA ELETRICA"
    await _criar_regra(client, empresa, agencia_id, conta_id, "BOLETO", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["associadas"] >= 1


@pytest.mark.asyncio
async def test_neo_desempata_pela_regra_mais_especifica(client, db, tenant, usuario, empresa):
    """Com duas regras candidatas, vence a mais específica — não a que o banco devolveu primeiro.

    "BOLETO" e "BOLETO ENERGIA" casam ambas por substring com
    "BOLETO ENERGIA ELETRICA". A regra curta é criada primeiro de propósito: sem
    ORDER BY o Postgres tende a devolver na ordem de inserção e a genérica venceria,
    jogando a transação numa conta contábil diferente.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_generica = await _setup_base(client, db, empresa, csrf)

    conta_especifica = PlanoConta(
        empresa_id=empresa.id, codigo="4.1.1", descricao="Energia Elétrica", tipo="despesa"
    )
    db.add(conta_especifica)
    await db.flush()

    generica = await _criar_regra(
        client, empresa, agencia_id, conta_generica, "BOLETO", "D", csrf
    )
    especifica = await _criar_regra(
        client, empresa, agencia_id, str(conta_especifica.id), "BOLETO ENERGIA", "D", csrf
    )

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    decisoes = (
        await client.get(f"/api/v1/empresas/{empresa.id}/neo/decisoes?resultado=associada")
    ).json()["items"]
    boleto = [d for d in decisoes if d["transacao_descricao"] == "BOLETO ENERGIA ELETRICA"]
    assert len(boleto) == 1

    assert boleto[0]["estrategia"] == "substring"
    assert boleto[0]["regra_id"] == especifica["id"], "a regra genérica venceu a específica"
    assert boleto[0]["regra_id"] != generica["id"]


@pytest.mark.asyncio
async def test_neo_carrega_regras_em_ordem_estavel(client, db, tenant, usuario, empresa):
    """As regras chegam ao matching ordenadas da mais longa para a mais curta."""
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)

    for historico in ("BOL", "BOLETO ENERGIA", "BOLETO"):
        await _criar_regra(client, empresa, agencia_id, conta_id, historico, "D", csrf)

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    regras = await engine._carregar_regras(None)

    tamanhos = [len(r.historico) for r in regras]
    assert tamanhos == sorted(tamanhos, reverse=True)
    assert [r.historico for r in regras][:3] == ["BOLETO ENERGIA", "BOLETO", "BOL"]


@pytest.mark.asyncio
async def test_neo_sem_regra_registra(client, db, tenant, usuario, empresa):
    """Transações sem regra devem ter resultado=sem_regra nas decisões."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    # Não cria nenhuma regra

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["sem_regra"] >= 1
    assert r.json()["associadas"] == 0


@pytest.mark.asyncio
async def test_neo_idempotente(client, db, tenant, usuario, empresa):
    """Transações já processadas não são reprocessadas numa segunda execução."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf)

    r1 = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    r2 = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 200
    assert r2.status_code == 200

    # A transação que foi associada na 1ª execução não gera nova associação na 2ª.
    # (sem_regra permanecem pendentes para poder ser recuperadas quando novas regras forem criadas.)
    assert r1.json()["associadas"] >= 1
    assert r2.json()["associadas"] == 0


# ── Listar decisões


@pytest.mark.asyncio
async def test_listar_decisoes_neo(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    r = await client.get(f"/api/v1/empresas/{empresa.id}/neo/decisoes")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert "items" in body


@pytest.mark.asyncio
async def test_neo_sem_csrf_rejeita(client, tenant, usuario, empresa):
    await _login(client, tenant, usuario)
    r = await client.post(_processar_url(empresa.id), json={})
    assert r.status_code == 403
