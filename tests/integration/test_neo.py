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
    Contraparte,
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


_OFX_DOIS_MESES = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240301
<TRNAMT>-150.00
<FITID>NEO_MES_TX1
<MEMO>PGTO FORNECEDOR MES TESTE
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240415
<TRNAMT>-150.00
<FITID>NEO_MES_TX2
<MEMO>PGTO FORNECEDOR MES TESTE
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


@pytest.mark.asyncio
async def test_neo_processar_filtra_por_mes(client, db, tenant, usuario, empresa):
    """mes=AAAA-MM restringe o processamento àquele mês — a transação de
    outro mês permanece pendente até um processamento futuro (com ou sem
    filtro) alcançá-la."""
    csrf = await _login(client, tenant, usuario)
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "BRADESCO", "agencia": "0004", "numero": "44444"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()
    conta = PlanoConta(empresa_id=empresa.id, codigo="4.1.1", descricao="Despesas Fornecedor", tipo="despesa")
    db.add(conta)
    await db.flush()

    await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("e.ofx", io.BytesIO(_OFX_DOIS_MESES.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    await _criar_regra(client, empresa, agencia["id"], str(conta.id), "PGTO FORNECEDOR MES TESTE", "D", csrf)

    r_marco = await client.post(
        _processar_url(empresa.id),
        json={"mes": "2024-03"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_marco.status_code == 200
    body_marco = r_marco.json()
    assert body_marco["total_pendentes"] == 1
    assert body_marco["associadas"] == 1

    # A transação de abril não foi tocada — continua pendente.
    pendentes = (
        await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id, Transacao.status == "pendente"))
    ).scalars().all()
    assert len(pendentes) == 1
    assert pendentes[0].data.month == 4

    r_abril = await client.post(
        _processar_url(empresa.id),
        json={"mes": "2024-04"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_abril.status_code == 200
    assert r_abril.json()["associadas"] == 1

    pendentes_depois = (
        await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id, Transacao.status == "pendente"))
    ).scalars().all()
    assert pendentes_depois == []


@pytest.mark.asyncio
async def test_neo_processar_mes_formato_invalido_rejeita(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _processar_url(empresa.id),
        json={"mes": "03-2024"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


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
async def test_neo_normaliza_historico_e_descricao_preservando_extrato(
    client, db, tenant, usuario, empresa
):
    """RegistroContabil.historico/descricao saem em maiúsculas e sem espaços
    duplicados, mas historico_extrato preserva o texto bruto do banco."""
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    _, conta_id = await _setup_base(client, db, empresa, csrf)
    transacao = (
        await db.execute(
            select(Transacao).where(
                Transacao.empresa_id == empresa.id,
                Transacao.historico == "BOLETO ENERGIA ELETRICA",
            )
        )
    ).scalar_one()

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    await engine.registrar_partidas_manuais(
        transacao, conta_id=UUID(conta_id), descricao="  pgto   ref\nFornecedor Alfa  "
    )
    await db.flush()

    partidas = (
        await db.execute(
            select(RegistroContabil).where(
                RegistroContabil.transacao_id == transacao.id,
                RegistroContabil.deleted_at.is_(None),
            )
        )
    ).scalars().all()

    assert len(partidas) == 2
    lancamento = next(p for p in partidas if p.conta_id == UUID(conta_id))
    assert lancamento.descricao == "PGTO REF FORNECEDOR ALFA"
    assert lancamento.historico == "BOLETO ENERGIA ELETRICA"
    # A evidência bruta do extrato não é tocada pela normalização.
    assert lancamento.historico_extrato == "BOLETO ENERGIA ELETRICA"


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


@pytest.mark.asyncio
async def test_selecionar_comprovante_candidato_nao_associa(
    client, db, tenant, usuario, empresa
):
    """A seleção é só leitura — não muta transacao_id do candidato."""
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
    comprovante = Comprovante(
        empresa_id=empresa.id,
        agencia_id=debito.agencia_id,
        data_pagamento=debito.data,
        valor_pago=debito.valor,
    )
    db.add(comprovante)
    await db.flush()

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    candidato = await engine._selecionar_comprovante_candidato(debito)
    assert candidato is not None
    assert candidato.id == comprovante.id
    assert comprovante.transacao_id is None  # seleção não associa

    # selecionar de novo (sem vincular) continua encontrando o mesmo candidato
    candidato_de_novo = await engine._selecionar_comprovante_candidato(debito)
    assert candidato_de_novo.id == comprovante.id

    await engine._vincular_comprovante(debito, candidato)
    assert comprovante.transacao_id == debito.id


@pytest.mark.asyncio
async def test_selecionar_nota_candidata_nao_associa(client, db, tenant, usuario, empresa):
    """A seleção é só leitura — não muta transacao_id/status do candidato."""
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
    nota = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-SELECAO",
        cnpj_emitente="11.111.111/0001-11",
        cnpj_destinatario=empresa.cnpj,
        valor=debito.valor,
        data_emissao=debito.data,
        dedup_key="teste-selecao-nota",
    )
    db.add(nota)
    await db.flush()

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    engine._empresa_cnpj = empresa.cnpj
    candidata = await engine._selecionar_nota_candidata(debito)
    assert candidata is not None
    assert candidata.id == nota.id
    assert nota.transacao_id is None
    assert nota.status == "pendente"

    await engine._vincular_nota(debito, candidata)
    assert nota.transacao_id == debito.id
    assert nota.status == "associada"


async def _criar_conta_generica(db, empresa, codigo="4.9.0") -> PlanoConta:
    conta = PlanoConta(empresa_id=empresa.id, codigo=codigo, descricao="Conta de teste", tipo="despesa")
    db.add(conta)
    await db.flush()
    return conta


async def _criar_contraparte(db, empresa, conta, documento="52540787000188", **over) -> Contraparte:
    contraparte = Contraparte(
        empresa_id=empresa.id,
        tipo=over.pop("tipo", "fornecedor"),
        documento=documento,
        razao_social=over.pop("razao_social", "Axel Tecnologia Ltda"),
        conta_contabil_id=conta.id,
        origem="manual",
        confirmado_em=datetime.now(UTC),
        **over,
    )
    db.add(contraparte)
    await db.flush()
    return contraparte


@pytest.mark.asyncio
async def test_shadow_resolve_contraparte_por_nota_fiscal(
    client, db, tenant, usuario, empresa
):
    """A resolução em shadow mode encontra a contraparte pelo CNPJ do lado
    correto da nota (emitente, numa transação de débito), sem alterar nada."""
    from src.db.models import Regra
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_regra = await _setup_base(client, db, empresa, csrf)
    conta_contraparte = await _criar_conta_generica(db, empresa, codigo="4.9.1")

    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    debito = transacoes["BOLETO ENERGIA ELETRICA"]
    nota = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-777",
        cnpj_emitente="52.540.787/0001-88",
        cnpj_destinatario=empresa.cnpj,
        valor=debito.valor,
        data_emissao=debito.data,
        dedup_key="teste-shadow-nota",
    )
    db.add(nota)
    await db.flush()
    contraparte = await _criar_contraparte(
        db, empresa, conta_contraparte, documento="52540787000188"
    )

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    engine._empresa_cnpj = empresa.cnpj
    regra = Regra(conta_id=UUID(conta_regra))

    resolucao = await engine._resolver_contraparte_sombra(
        debito, regra, comprovante_candidato=None, nota_candidata=nota
    )

    assert resolucao is not None
    assert resolucao.contraparte_id == contraparte.id
    assert resolucao.origem_evidencia == "nota_fiscal"
    assert resolucao.conta_contraparte_id == conta_contraparte.id
    assert resolucao.conta_divergente is True  # regra usa conta_regra, contraparte usa outra
    assert resolucao.historico_sugerido == "PGTO REF NF NF-777 - AXEL TECNOLOGIA LTDA"
    # nada foi mutado pela resolução
    assert nota.transacao_id is None
    assert nota.status == "pendente"


@pytest.mark.asyncio
async def test_shadow_resolve_contraparte_por_comprovante_quando_sem_nota(
    client, db, tenant, usuario, empresa
):
    from src.db.models import Regra
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_regra = await _setup_base(client, db, empresa, csrf)
    conta_contraparte = await _criar_conta_generica(db, empresa, codigo="4.9.2")

    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    debito = transacoes["BOLETO ENERGIA ELETRICA"]
    comprovante = Comprovante(
        empresa_id=empresa.id,
        agencia_id=debito.agencia_id,
        data_pagamento=debito.data,
        valor_pago=debito.valor,
        cpf_cnpj="12.810.326/0001-63",
    )
    db.add(comprovante)
    await db.flush()
    contraparte = await _criar_contraparte(
        db, empresa, conta_contraparte,
        documento="12810326000163", razao_social="Cargo Time Transportes",
    )

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    regra = Regra(conta_id=UUID(conta_regra))

    resolucao = await engine._resolver_contraparte_sombra(
        debito, regra, comprovante_candidato=comprovante, nota_candidata=None
    )

    assert resolucao is not None
    assert resolucao.contraparte_id == contraparte.id
    assert resolucao.origem_evidencia == "comprovante"
    assert resolucao.historico_sugerido == "PGTO REF CARGO TIME TRANSPORTES"
    assert comprovante.transacao_id is None  # nada foi mutado


@pytest.mark.asyncio
async def test_shadow_sem_contraparte_cadastrada_retorna_none(
    client, db, tenant, usuario, empresa
):
    from src.db.models import Regra
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_regra = await _setup_base(client, db, empresa, csrf)

    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    debito = transacoes["BOLETO ENERGIA ELETRICA"]
    comprovante = Comprovante(
        empresa_id=empresa.id,
        agencia_id=debito.agencia_id,
        data_pagamento=debito.data,
        valor_pago=debito.valor,
        cpf_cnpj="00.000.000/0001-00",  # nenhuma contraparte com esse documento
    )
    db.add(comprovante)
    await db.flush()

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    regra = Regra(conta_id=UUID(conta_regra))

    resolucao = await engine._resolver_contraparte_sombra(
        debito, regra, comprovante_candidato=comprovante, nota_candidata=None
    )
    assert resolucao is None


@pytest.mark.asyncio
async def test_shadow_mode_nao_altera_lancamento_real(
    client, db, tenant, usuario, empresa
):
    """Fim a fim: mesmo com uma contraparte cadastrada apontando pra outra
    conta, o lançamento criado pelo processamento continua usando a conta e
    o histórico decididos pela regra — shadow mode só observa."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_regra = await _setup_base(client, db, empresa, csrf)
    conta_contraparte = await _criar_conta_generica(db, empresa, codigo="4.9.3")

    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    debito = transacoes["BOLETO ENERGIA ELETRICA"]
    nota = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-888",
        cnpj_emitente="52.540.787/0001-88",
        cnpj_destinatario=empresa.cnpj,
        valor=debito.valor,
        data_emissao=debito.data,
        dedup_key="teste-shadow-nao-altera",
    )
    db.add(nota)
    await db.flush()
    await _criar_contraparte(db, empresa, conta_contraparte, documento="52540787000188")
    await _criar_regra(client, empresa, agencia_id, conta_regra, "BOLETO", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    partidas = (
        await db.execute(
            select(RegistroContabil).where(
                RegistroContabil.transacao_id == debito.id,
                RegistroContabil.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    lancamento = next(p for p in partidas if p.conta_id == UUID(conta_regra))
    assert lancamento is not None  # a conta usada foi a da regra, não a da contraparte
    assert lancamento.conta_id != conta_contraparte.id

    # o auto-match de nota fiscal continua funcionando normalmente
    await db.refresh(nota)
    assert nota.transacao_id == debito.id
    assert nota.status == "associada"


@pytest.mark.asyncio
async def test_resolver_contraparte_candidata_funciona_sem_regra(
    client, db, tenant, usuario, empresa
):
    """O núcleo da resolução não depende de haver regra — é o que a Entrega 5
    estendida usa para transações sem_regra (crédito, via nota fiscal de saída)."""
    from src.domain.neo.engine import NeoEngine

    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)
    conta_contraparte = await _criar_conta_generica(db, empresa, codigo="4.9.4")

    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    sem_regra_transacao = transacoes["DEPOSITO AVULSO DESCONHECIDO"]  # crédito, sem regra
    nota = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-999",
        cnpj_emitente=empresa.cnpj,
        cnpj_destinatario="52.540.787/0001-88",
        valor=sem_regra_transacao.valor,
        data_emissao=sem_regra_transacao.data,
        dedup_key="teste-shadow-sem-regra",
    )
    db.add(nota)
    await db.flush()
    contraparte = await _criar_contraparte(
        db, empresa, conta_contraparte, documento="52540787000188",
        tipo="cliente", razao_social="Axel Tecnologia Ltda",
    )

    engine = NeoEngine(db=db, empresa_id=empresa.id)
    engine._empresa_cnpj = empresa.cnpj

    comprovante_candidato = await engine._selecionar_comprovante_candidato(sem_regra_transacao)
    assert comprovante_candidato is None  # transação de crédito, comprovante nunca casa
    nota_candidata = await engine._selecionar_nota_candidata(sem_regra_transacao)
    assert nota_candidata is not None

    encontrado = await engine._resolver_contraparte_candidata(
        sem_regra_transacao, comprovante_candidato, nota_candidata
    )
    assert encontrado is not None
    contraparte_encontrada, origem_evidencia, numero_nf = encontrado
    assert contraparte_encontrada.id == contraparte.id
    assert origem_evidencia == "nota_fiscal"
    assert numero_nf == "NF-999"


@pytest.mark.asyncio
async def test_neo_classifica_sem_regra_via_contraparte(
    client, db, tenant, usuario, empresa
):
    """Ativação dos itens 1+2 do PDF (2026-08-18): uma transação sem regra,
    mas com contraparte identificável via nota fiscal, é classificada de
    verdade — lançamento criado na conta da contraparte, histórico no
    formato pedido, documento associado, transação processada."""
    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)
    conta_contraparte = await _criar_conta_generica(db, empresa, codigo="4.9.6")

    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    transacao = transacoes["DEPOSITO AVULSO DESCONHECIDO"]  # crédito, sem regra
    nota = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-321",
        cnpj_emitente=empresa.cnpj,
        cnpj_destinatario="52.540.787/0001-88",
        valor=transacao.valor,
        data_emissao=transacao.data,
        dedup_key="teste-classificacao-contraparte",
    )
    db.add(nota)
    await db.flush()
    contraparte = await _criar_contraparte(
        db, empresa, conta_contraparte, documento="52540787000188",
        tipo="cliente", razao_social="Axel Tecnologia Ltda",
    )

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    body = r.json()
    assert body["classificadas_por_contraparte"] == 1
    assert body["associadas"] >= 1
    assert body["sem_regra"] == 2  # as outras 2 transações do OFX continuam sem regra/contraparte

    await db.refresh(transacao)
    assert transacao.status == "processada"

    await db.refresh(nota)
    assert nota.transacao_id == transacao.id
    assert nota.status == "associada"

    partidas = (
        await db.execute(
            select(RegistroContabil).where(
                RegistroContabil.transacao_id == transacao.id,
                RegistroContabil.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    assert len(partidas) == 2
    lancamento = next(p for p in partidas if p.conta_id == conta_contraparte.id)
    assert lancamento.dc == "C"
    assert lancamento.descricao == "AXEL TECNOLOGIA LTDA"
    assert lancamento.historico == "RECEBIMENTO REF NF NF-321 - AXEL TECNOLOGIA LTDA"
    assert lancamento.historico_extrato == "DEPOSITO AVULSO DESCONHECIDO"  # extrato bruto preservado

    decisao = (
        await db.execute(
            select(NeoDecisao).where(NeoDecisao.transacao_id == transacao.id)
        )
    ).scalar_one()
    assert decisao.resultado == "associada"
    assert decisao.estrategia == "contraparte"
    assert decisao.regra_id is None


@pytest.mark.asyncio
async def test_neo_classificacao_por_contraparte_nunca_disputa_com_regra_existente(
    client, db, tenant, usuario, empresa
):
    """Quando já existe uma regra que classifica a transação, a contraparte
    NUNCA entra em jogo — mesmo que aponte para uma conta diferente."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_regra = await _setup_base(client, db, empresa, csrf)
    conta_contraparte = await _criar_conta_generica(db, empresa, codigo="4.9.7")

    transacoes = {
        t.historico: t
        for t in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    debito = transacoes["BOLETO ENERGIA ELETRICA"]
    nota = NotaFiscal(
        empresa_id=empresa.id,
        tipo="nfe",
        numero="NF-999",
        cnpj_emitente="52.540.787/0001-88",
        cnpj_destinatario=empresa.cnpj,
        valor=debito.valor,
        data_emissao=debito.data,
        dedup_key="teste-nao-disputa-regra",
    )
    db.add(nota)
    await db.flush()
    await _criar_contraparte(db, empresa, conta_contraparte, documento="52540787000188")
    await _criar_regra(client, empresa, agencia_id, conta_regra, "BOLETO", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["classificadas_por_contraparte"] == 0

    partidas = (
        await db.execute(
            select(RegistroContabil).where(RegistroContabil.transacao_id == debito.id)
        )
    ).scalars().all()
    lancamento = next(p for p in partidas if p.conta_id == UUID(conta_regra))
    assert lancamento is not None
    assert lancamento.conta_id != conta_contraparte.id

    decisao = (
        await db.execute(select(NeoDecisao).where(NeoDecisao.transacao_id == debito.id))
    ).scalar_one()
    assert decisao.estrategia != "contraparte"
    assert decisao.regra_id is not None


@pytest.mark.asyncio
async def test_shadow_sem_regra_sem_contraparte_nao_quebra_processamento(
    client, db, tenant, usuario, empresa
):
    """Caminho comum hoje (cadastro de contrapartes vazio): sem_regra continua
    funcionando normalmente, sem erro nenhum vindo da tentativa de resolução."""
    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["sem_regra"] >= 1


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


def _decisoes_url(empresa_id) -> str:
    return f"/api/v1/empresas/{empresa_id}/neo/decisoes"


@pytest.mark.asyncio
async def test_listar_decisoes_response_inclui_page_e_page_size(
    client, tenant, usuario, empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_decisoes_url(empresa.id) + "?page=2&page_size=10")
    assert r.status_code == 200
    assert r.json()["page"] == 2
    assert r.json()["page_size"] == 10


@pytest.mark.asyncio
async def test_listar_decisoes_filtro_termo_busca_historico_e_regra(
    client, db, tenant, usuario, empresa
):
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "BOLETO", "D", csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    por_historico = await client.get(_decisoes_url(empresa.id) + "?termo=CLIENTE ALFA")
    assert por_historico.status_code == 200
    itens = por_historico.json()["items"]
    assert [i["transacao_descricao"] for i in itens] == ["TED RECEBIDA CLIENTE ALFA"]

    por_regra = await client.get(_decisoes_url(empresa.id) + "?termo=Regra BOLETO")
    itens_regra = por_regra.json()["items"]
    assert [i["transacao_descricao"] for i in itens_regra] == ["BOLETO ENERGIA ELETRICA"]

    sem_match = await client.get(_decisoes_url(empresa.id) + "?termo=inexistente-xyz")
    assert sem_match.json()["items"] == []


@pytest.mark.asyncio
async def test_listar_decisoes_termo_com_percent_nao_vira_wildcard(
    client, db, tenant, usuario, empresa
):
    """'%' digitado pelo usuário é texto literal, não coringa de SQL."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    r = await client.get(_decisoes_url(empresa.id) + "?termo=100%")
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_listar_decisoes_filtro_estrategia(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "BOLETO", "D", csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    exatas = await client.get(_decisoes_url(empresa.id) + "?estrategia=exato")
    assert {i["transacao_descricao"] for i in exatas.json()["items"]} == {
        "TED RECEBIDA CLIENTE ALFA"
    }

    substrings = await client.get(_decisoes_url(empresa.id) + "?estrategia=substring")
    assert {i["transacao_descricao"] for i in substrings.json()["items"]} == {
        "BOLETO ENERGIA ELETRICA"
    }


@pytest.mark.asyncio
async def test_listar_decisoes_filtro_dc(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    debitos = await client.get(_decisoes_url(empresa.id) + "?dc=D")
    itens_d = debitos.json()["items"]
    assert itens_d
    assert all(i["transacao_dc"] == "D" for i in itens_d)

    creditos = await client.get(_decisoes_url(empresa.id) + "?dc=C")
    itens_c = creditos.json()["items"]
    assert itens_c
    assert all(i["transacao_dc"] == "C" for i in itens_c)


@pytest.mark.asyncio
async def test_listar_decisoes_filtro_agencia_e_conta(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    por_agencia = await client.get(_decisoes_url(empresa.id) + f"?agencia_id={agencia_id}")
    assert por_agencia.json()["items"]

    por_conta = await client.get(_decisoes_url(empresa.id) + f"?conta_id={conta_id}")
    assert any(
        i["transacao_descricao"] == "TED RECEBIDA CLIENTE ALFA"
        for i in por_conta.json()["items"]
    )

    agencia_inexistente = "00000000-0000-0000-0000-000000000099"
    vazio = await client.get(_decisoes_url(empresa.id) + f"?agencia_id={agencia_inexistente}")
    assert vazio.status_code == 200
    assert vazio.json()["items"] == []


@pytest.mark.asyncio
async def test_listar_decisoes_filtro_mes(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "BRADESCO", "agencia": "0005", "numero": "55555"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()
    conta = PlanoConta(
        empresa_id=empresa.id, codigo="4.5.1", descricao="Despesas Mes", tipo="despesa"
    )
    db.add(conta)
    await db.flush()
    await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("e.ofx", io.BytesIO(_OFX_DOIS_MESES.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    await _criar_regra(
        client, empresa, agencia["id"], str(conta.id), "PGTO FORNECEDOR MES TESTE", "D", csrf
    )
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    marco = await client.get(_decisoes_url(empresa.id) + "?mes=2024-03")
    assert marco.json()["total"] == 1

    abril = await client.get(_decisoes_url(empresa.id) + "?mes=2024-04")
    assert abril.json()["total"] == 1

    invalido = await client.get(_decisoes_url(empresa.id) + "?mes=13-2024")
    assert invalido.status_code == 422


@pytest.mark.asyncio
async def test_neo_sem_csrf_rejeita(client, tenant, usuario, empresa):
    await _login(client, tenant, usuario)
    r = await client.post(_processar_url(empresa.id), json={})
    assert r.status_code == 403


# ── Matching tolerante a variações do histórico bancário ─────────────────────
#
# Reportado pelo escritório (2026-08-18): uma regra "TARIFA" não pegava
# "TARIFA COM LIQUIDAÇÃO" nem "TARIFA COM R LIQUIDAÇÃO", obrigando a cadastrar
# uma regra por variação de texto do banco.

_OFX_TARIFAS = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240301
<TRNAMT>-12.50
<FITID>TARIFA_1
<MEMO>TARIFA COM LIQUIDAÇÃO
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240301
<TRNAMT>-9.90
<FITID>TARIFA_2
<MEMO>Tarifa com R liquidacao
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240302
<TRNAMT>-4.20
<FITID>TARIFA_3
<MEMO>TARIFA/PACOTE DE SERVICOS
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


async def _setup_tarifas(client, db, empresa, csrf):
    agencia = (
        await client.post(
            f"/api/v1/empresas/{empresa.id}/agencias",
            json={"banco_sigla": "ITAU", "agencia": "0009", "numero": "99999"},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    conta = PlanoConta(
        empresa_id=empresa.id, codigo="4.1.9", descricao="Tarifas Bancárias", tipo="despesa"
    )
    db.add(conta)
    await db.flush()

    r = await client.post(
        f"/api/v1/empresas/{empresa.id}/extrato/importar?agencia_id={agencia['id']}",
        files={"arquivo": ("t.ofx", io.BytesIO(_OFX_TARIFAS.encode()), "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    return agencia["id"], str(conta.id)


@pytest.mark.asyncio
async def test_uma_regra_por_palavra_chave_cobre_todas_as_variacoes(
    client, db, tenant, usuario, empresa
):
    """Uma regra "TARIFA" classifica as três variações que o banco escreve.

    É o caso exato relatado: com acento, sem acento, com palavra extra no meio
    e com barra separando — tudo com uma única regra cadastrada.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)

    await _criar_regra(client, empresa, agencia_id, conta_id, "TARIFA", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["associadas"] == 3
    assert r.json()["sem_regra"] == 0


@pytest.mark.asyncio
async def test_regra_com_acento_casa_historico_sem_acento(client, db, tenant, usuario, empresa):
    """A regra é digitada com acento e o extrato vem sem — e vice-versa."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)

    await _criar_regra(client, empresa, agencia_id, conta_id, "TARIFA COM LIQUIDAÇÃO", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    decisoes = (
        await client.get(_decisoes_url(empresa.id) + "?resultado=associada")
    ).json()["items"]
    historicos = {d["transacao_descricao"] for d in decisoes}
    assert "TARIFA COM LIQUIDAÇÃO" in historicos


@pytest.mark.asyncio
async def test_palavra_extra_no_meio_do_historico_ainda_casa(
    client, db, tenant, usuario, empresa
):
    """A regra "TARIFA COM LIQUIDACAO" pega "Tarifa com R liquidacao".

    O "R" que o banco enfia no meio quebrava substring e prefixo — era o que
    obrigava a cadastrar uma regra por variação.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)

    await _criar_regra(client, empresa, agencia_id, conta_id, "TARIFA COM LIQUIDACAO", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    decisoes = (
        await client.get(_decisoes_url(empresa.id) + "?resultado=associada")
    ).json()["items"]
    por_historico = {d["transacao_descricao"]: d for d in decisoes}
    assert "Tarifa com R liquidacao" in por_historico
    assert por_historico["Tarifa com R liquidacao"]["estrategia"] == "todas_palavras"


@pytest.mark.asyncio
async def test_todas_palavras_nao_atropela_regra_mais_especifica(
    client, db, tenant, usuario, empresa
):
    """A estratégia nova é a última da fila: quem casa exato continua vencendo."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_generica = await _setup_tarifas(client, db, empresa, csrf)

    conta_especifica = PlanoConta(
        empresa_id=empresa.id, codigo="4.1.10", descricao="Pacote de Serviços", tipo="despesa"
    )
    db.add(conta_especifica)
    await db.flush()

    generica = await _criar_regra(
        client, empresa, agencia_id, conta_generica, "TARIFA", "D", csrf
    )
    especifica = await _criar_regra(
        client, empresa, agencia_id, str(conta_especifica.id), "TARIFA PACOTE", "D", csrf
    )

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    decisoes = (
        await client.get(_decisoes_url(empresa.id) + "?resultado=associada")
    ).json()["items"]
    pacote = [d for d in decisoes if d["transacao_descricao"] == "TARIFA/PACOTE DE SERVICOS"]
    assert len(pacote) == 1
    assert pacote[0]["regra_id"] == especifica["id"]
    assert pacote[0]["regra_id"] != generica["id"]


# ── Decisão "sem regra" órfã ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transacao_classificada_depois_sai_da_lista_sem_regra(
    client, db, tenant, usuario, empresa
):
    """O caso do "Erro ao associar" relatado pelo escritório.

    Rodada 1 sem regra → a transação entra em "Sem Regra". A regra é criada e o
    NEO roda de novo → a decisão antiga precisa ser *encerrada*, não duplicada.
    Antes, a linha 'sem_regra' ficava para trás e a tela continuava oferecendo
    associar uma transação já contabilizada.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.json()["sem_regra"] == 3

    sem_regra = (await client.get(_decisoes_url(empresa.id) + "?resultado=sem_regra")).json()
    assert sem_regra["total"] == 3

    await _criar_regra(client, empresa, agencia_id, conta_id, "TARIFA", "D", csrf)
    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.json()["associadas"] == 3

    sem_regra = (await client.get(_decisoes_url(empresa.id) + "?resultado=sem_regra")).json()
    assert sem_regra["total"] == 0, "decisão 'sem_regra' ficou órfã depois da classificação"

    associadas = (await client.get(_decisoes_url(empresa.id) + "?resultado=associada")).json()
    assert associadas["total"] == 3, "a decisão foi duplicada em vez de encerrada"


@pytest.mark.asyncio
async def test_associar_manual_em_transacao_ja_contabilizada_limpa_residuo(
    client, db, tenant, usuario, empresa
):
    """Listagem velha em mãos: a rota responde 409 com mensagem que diz o que
    houve, em vez do 422 genérico "não está mais pendente"."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)

    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    decisao_id = (
        await client.get(_decisoes_url(empresa.id) + "?resultado=sem_regra")
    ).json()["items"][0]["id"]

    # Simula a tela desatualizada: a transação foi contabilizada depois que a
    # listagem carregou.
    decisao = (
        await db.execute(select(NeoDecisao).where(NeoDecisao.id == UUID(decisao_id)))
    ).scalar_one()
    transacao = (
        await db.execute(select(Transacao).where(Transacao.id == decisao.transacao_id))
    ).scalar_one()
    transacao.status = "processada"
    await db.flush()

    r = await client.post(
        _decisoes_url(empresa.id) + f"/{decisao_id}/associar-manual",
        json={"conta_id": conta_id, "descricao": "Tarifa bancária"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409
    assert "já foi contabilizada" in r.json()["message"]


# ── Filtros da tela de busca do NEO ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_filtro_dc_aceita_palavra_por_extenso(client, db, tenant, usuario, empresa):
    """O front manda "débito"/"crédito" em alguns pontos — antes virava lista vazia."""
    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    por_letra = (await client.get(_decisoes_url(empresa.id) + "?dc=D")).json()
    assert por_letra["total"] > 0
    for variacao in ("debito", "Débito", "DEBITOS", "d"):
        r = await client.get(_decisoes_url(empresa.id) + f"?dc={variacao}")
        assert r.status_code == 200, variacao
        assert r.json()["total"] == por_letra["total"], variacao


@pytest.mark.asyncio
async def test_filtro_dc_invalido_responde_422(client, tenant, usuario, empresa):
    """Filtro errado precisa doer, não devolver lista vazia em silêncio."""
    await _login(client, tenant, usuario)
    r = await client.get(_decisoes_url(empresa.id) + "?dc=ambos")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_filtro_resultado_e_estrategia_invalidos_respondem_422(
    client, tenant, usuario, empresa
):
    await _login(client, tenant, usuario)
    assert (
        await client.get(_decisoes_url(empresa.id) + "?resultado=pendente")
    ).status_code == 422
    assert (
        await client.get(_decisoes_url(empresa.id) + "?estrategia=fuzzy")
    ).status_code == 422


@pytest.mark.asyncio
async def test_filtro_por_faixa_de_valor(client, db, tenant, usuario, empresa):
    """O extrato de teste tem 5000,00 (C), 200,00 (D) e 100,00 (C)."""
    csrf = await _login(client, tenant, usuario)
    await _setup_base(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    assert (await client.get(_decisoes_url(empresa.id))).json()["total"] == 3
    assert (await client.get(_decisoes_url(empresa.id) + "?valor_min=150")).json()["total"] == 2
    assert (await client.get(_decisoes_url(empresa.id) + "?valor_max=150")).json()["total"] == 1
    faixa = await client.get(_decisoes_url(empresa.id) + "?valor_min=150&valor_max=1000")
    assert faixa.json()["total"] == 1

    invertida = await client.get(_decisoes_url(empresa.id) + "?valor_min=900&valor_max=100")
    assert invertida.status_code == 422


@pytest.mark.asyncio
async def test_busca_por_termo_ignora_acento(client, db, tenant, usuario, empresa):
    """Quem digita "liquidacao" tem que achar "TARIFA COM LIQUIDAÇÃO" no extrato."""
    csrf = await _login(client, tenant, usuario)
    await _setup_tarifas(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    sem_acento = (await client.get(_decisoes_url(empresa.id) + "?termo=liquidacao")).json()
    com_acento = (await client.get(_decisoes_url(empresa.id) + "?termo=LIQUIDAÇÃO")).json()
    assert sem_acento["total"] == 2
    assert com_acento["total"] == 2
