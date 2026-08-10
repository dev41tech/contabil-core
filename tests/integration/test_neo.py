"""Testes de integração — NEO (motor de matching automático)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.db.models import (
    AuditLog,
    AgenciaBancaria,
    Comprovante,
    Empresa,
    NeoDecisao,
    NotaFiscal,
    PlanoConta,
    RegistroContabil,
    Transacao,
)

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


@pytest.mark.asyncio
async def test_neo_cria_duas_partidas_balanceadas(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(
        client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf
    )

    resposta = await client.post(
        _processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resposta.status_code == 200

    transacao = (
        await db.execute(
            select(Transacao).where(
                Transacao.empresa_id == empresa.id,
                Transacao.historico == "TED RECEBIDA CLIENTE ALFA",
            )
        )
    ).scalar_one()
    partidas = (
        await db.execute(
            select(RegistroContabil).where(
                RegistroContabil.transacao_id == transacao.id,
                RegistroContabil.deleted_at.is_(None),
            )
        )
    ).scalars().all()

    assert len(partidas) == 2
    assert {p.dc for p in partidas} == {"D", "C"}
    assert len({p.lancamento_id for p in partidas}) == 1
    assert sum((p.valor for p in partidas if p.dc == "D"), Decimal("0")) == sum(
        (p.valor for p in partidas if p.dc == "C"), Decimal("0")
    )
    agencia = await db.get(AgenciaBancaria, transacao.agencia_id)
    assert agencia.conta_contabil_id in {p.conta_id for p in partidas}


@pytest.mark.asyncio
async def test_neo_sem_regra_nao_duplica_decisao(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)

    for _ in range(3):
        resposta = await client.post(
            _processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf}
        )
        assert resposta.status_code == 200

    transacao = (
        await db.execute(
            select(Transacao).where(
                Transacao.empresa_id == empresa.id,
                Transacao.historico == "DEPOSITO AVULSO DESCONHECIDO",
            )
        )
    ).scalar_one()
    decisoes = (
        await db.execute(
            select(NeoDecisao).where(
                NeoDecisao.transacao_id == transacao.id,
                NeoDecisao.resultado == "sem_regra",
            )
        )
    ).scalars().all()
    assert len(decisoes) == 1


@pytest.mark.asyncio
async def test_autoassociacao_nao_reutiliza_comprovante_e_exige_debito(
    client, db, tenant, usuario, empresa
):
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)
    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    debito = transacoes["BOLETO ENERGIA ELETRICA"]
    credito = transacoes["TED RECEBIDA CLIENTE ALFA"]
    outro_debito = Transacao(
        empresa_id=empresa.id,
        agencia_id=debito.agencia_id,
        data=debito.data,
        valor=debito.valor,
        historico="OUTRO PAGAMENTO MESMO VALOR",
        dc="D",
        hash_dedup="neo_outro_debito_200",
    )
    comprovante = Comprovante(
        empresa_id=empresa.id,
        agencia_id=debito.agencia_id,
        data_pagamento=datetime(2024, 3, 1, tzinfo=UTC),
        valor_pago=200,
    )
    db.add_all([outro_debito, comprovante])
    await db.flush()

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    assert await engine._tentar_associar_comprovante(credito) is False
    assert await engine._tentar_associar_comprovante(debito) is True
    assert await engine._tentar_associar_comprovante(outro_debito) is False
    assert comprovante.transacao_id == debito.id


@pytest.mark.asyncio
async def test_associacao_manual_valida_empresa_e_nao_recontabiliza(
    client, db, tenant, usuario, empresa
):
    csrf = await _login(client, tenant, usuario)
    _, conta_id = await _setup_base(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    decisoes = (
        await client.get(
            f"/api/v1/empresas/{empresa.id}/neo/decisoes?resultado=sem_regra"
        )
    ).json()["items"]
    decisao = next(
        d for d in decisoes if d["transacao_descricao"] == "DEPOSITO AVULSO DESCONHECIDO"
    )
    url = (
        f"/api/v1/empresas/{empresa.id}/neo/decisoes/"
        f"{decisao['id']}/associar-manual"
    )

    outra_empresa = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA EMPRESA LTDA",
        cnpj="98.765.432/0001-10",
        regime_tributario="simples_nacional",
    )
    db.add(outra_empresa)
    await db.flush()
    conta_outra = PlanoConta(
        empresa_id=outra_empresa.id,
        codigo="3.1.1",
        descricao="Receitas da outra empresa",
        tipo="receita",
    )
    db.add(conta_outra)
    await db.flush()

    conta_invalida = await client.post(
        url,
        json={"conta_id": str(conta_outra.id), "descricao": "Depósito manual"},
        headers={"X-CSRF-Token": csrf},
    )
    assert conta_invalida.status_code == 422

    associada = await client.post(
        url,
        json={"conta_id": conta_id, "descricao": "Depósito manual"},
        headers={"X-CSRF-Token": csrf},
    )
    repetida = await client.post(
        url,
        json={"conta_id": conta_id, "descricao": "Depósito manual"},
        headers={"X-CSRF-Token": csrf},
    )

    assert associada.status_code == 200
    assert repetida.status_code == 422
    partidas = (
        await db.execute(
            select(RegistroContabil).where(
                RegistroContabil.transacao_id == UUID(decisao["transacao_id"]),
                RegistroContabil.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    assert len(partidas) == 2
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.acao == "neo.associacao_manual",
                AuditLog.entidade_id == decisao["id"],
            )
        )
    ).scalar_one()
    assert audit.usuario_id == usuario.id
    assert audit.empresa_id == empresa.id


@pytest.mark.asyncio
async def test_autoassociacao_de_nota_respeita_direcao_financeira(
    client, db, tenant, usuario, empresa
):
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)
    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    debito = transacoes["BOLETO ENERGIA ELETRICA"]
    recebida = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-RECEBIDA",
        cnpj_emitente="11.111.111/0001-11",
        cnpj_destinatario=empresa.cnpj,
        valor=200,
        data_emissao=debito.data,
        dedup_key="teste-nota-recebida",
    )
    emitida_mesmo_valor = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-EMITIDA",
        cnpj_emitente=empresa.cnpj,
        cnpj_destinatario="22.222.222/0001-22",
        valor=200,
        data_emissao=debito.data,
        dedup_key="teste-nota-emitida",
    )
    db.add_all([recebida, emitida_mesmo_valor])
    await db.flush()

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    engine._empresa_cnpj = empresa.cnpj
    assert await engine._tentar_associar_nota_fiscal(debito) is True
    assert recebida.transacao_id == debito.id
    assert emitida_mesmo_valor.transacao_id is None


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
