"""Testes de integração — NEO (motor de matching automático)."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

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
    assert r.status_code == 202

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


async def _registrar_fila_agrupada(db, empresa, entradas: list[dict]):
    """Monta pendências diretamente para que o teste isole a consulta do motor."""
    agencias = [
        AgenciaBancaria(
            empresa_id=empresa.id,
            banco_sigla="ITAU",
            agencia=f"90{indice}",
            numero=f"9000{indice}",
        )
        for indice in range(2)
    ]
    db.add_all(agencias)
    await db.flush()

    transacoes = []
    for indice, entrada in enumerate(entradas):
        transacao = Transacao(
            empresa_id=empresa.id,
            agencia_id=agencias[entrada.get("agencia", 0)].id,
            data=entrada.get("data", date(2024, 3, indice + 1)),
            valor=Decimal(str(entrada.get("valor", "10.00"))),
            historico=entrada["historico"],
            dc=entrada.get("dc", "D"),
            status=entrada.get("status", "pendente"),
            hash_dedup=f"neo-grupo-{uuid4().hex}",
        )
        db.add(transacao)
        await db.flush()
        db.add(
            NeoDecisao(
                empresa_id=empresa.id,
                transacao_id=transacao.id,
                resultado="sem_regra",
                motivo="Sem regra para o teste de agrupamento",
            )
        )
        transacoes.append(transacao)
    await db.flush()
    return transacoes, agencias


def _pendencias_agrupadas_url(empresa_id) -> str:
    return f"/api/v1/empresas/{empresa_id}/neo/pendencias/agrupadas"


def _processar_url(empresa_id) -> str:
    return f"/api/v1/empresas/{empresa_id}/neo/processar"


async def _resultado_do_job(client, empresa_id, resposta) -> dict:
    """Lê o resultado persistido porque o POST agora trava o contrato 202/job.

    No transporte ASGI o BackgroundTasks termina antes de o ``post`` devolver
    ao teste, permitindo validar o ciclo completo sem espera artificial.
    """
    assert resposta.status_code == 202
    job = (
        await client.get(
            f"/api/v1/empresas/{empresa_id}/jobs/{resposta.json()['id']}"
        )
    ).json()
    assert job["status"] in {"concluido", "concluido_com_alertas"}
    return job["resultado"]


def _pendencias_url(empresa_id, acao: str) -> str:
    return f"/api/v1/empresas/{empresa_id}/neo/pendencias/{acao}"


# ── NEO pendências agrupadas


@pytest.mark.asyncio
async def test_pendencias_agrupa_tarifas_sem_fragmentar_por_agencia(
    client, db, tenant, usuario, empresa
):
    """A letra variável do banco não pode separar tarifas iguais, enquanto
    outro padrão deve continuar distinto e as agências devem virar metadado.

    Este é o caso que motivou o endpoint: fragmentá-lo manteria o trabalho
    repetitivo que o agrupamento pretende eliminar.
    """
    await _login(client, tenant, usuario)
    transacoes, agencias = await _registrar_fila_agrupada(
        db,
        empresa,
        [
            {"historico": "TARIFA COM LIQUIDAÇÃO", "valor": "10.00"},
            {
                "historico": "Tarifa com R liquidacao",
                "valor": "20.00",
                "agencia": 1,
            },
            {"historico": "TARIFA/PACOTE DE SERVICOS", "valor": "50.00"},
        ],
    )

    resposta = await client.get(_pendencias_agrupadas_url(empresa.id))
    assert resposta.status_code == 200
    body = resposta.json()
    assert body["total_pendentes"] == 3
    assert body["total_agrupadas"] == 3
    assert body["total_grupos"] == 2
    # Conjunto exato de chaves: a tela lê todas, e campo novo sem intenção
    # (ou removido) tem que aparecer aqui antes de chegar no front.
    assert set(body) == {
        "grupos",
        "total_pendentes",
        "total_agrupadas",
        "total_grupos",
        "parcial",
    }
    assert body["parcial"] is False

    tarifas = next(g for g in body["grupos"] if g["padrao"] == "tarifa com liquidacao")
    assert tarifas["quantidade"] == 2
    assert Decimal(tarifas["valor_total"]) == Decimal("30.00")
    assert set(tarifas["agencia_ids"]) == {str(agencia.id) for agencia in agencias}
    assert set(tarifas["transacao_ids"]) == {
        str(transacoes[0].id),
        str(transacoes[1].id),
    }
    assert {g["padrao"] for g in body["grupos"]} == {
        "tarifa com liquidacao",
        "tarifa pacote de",
    }

    limitado = (
        await client.get(
            _pendencias_agrupadas_url(empresa.id), params={"limite_grupos": 1}
        )
    ).json()
    assert limitado["total_pendentes"] == 3
    assert limitado["total_agrupadas"] == 2
    assert limitado["total_grupos"] == 2
    assert len(limitado["grupos"]) == 1


@pytest.mark.asyncio
async def test_pendencias_separa_mesmo_padrao_por_dc(
    client, db, tenant, usuario, empresa
):
    """Débito e crédito iguais precisam gerar grupos distintos porque nenhuma
    regra criada pela ação em lote pode operar nos dois lados.
    """
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(
        db,
        empresa,
        [
            {"historico": "PIX ENVIADO CLIENTE", "dc": "D"},
            {"historico": "PIX ENVIADO CLIENTE", "dc": "C"},
        ],
    )

    body = (await client.get(_pendencias_agrupadas_url(empresa.id))).json()
    assert body["total_grupos"] == 2
    assert {(grupo["padrao"], grupo["dc"]) for grupo in body["grupos"]} == {
        ("pix enviado cliente", "D"),
        ("pix enviado cliente", "C"),
    }


# ── Ações em lote da fila do NEO


@pytest.mark.asyncio
async def test_classificar_lote_processa_pendentes_e_ignora_retrato_velho(
    client, db, tenant, usuario, empresa
):
    """Um ID que saiu da fila não pode abortar as outras classificações.

    Este comportamento trava o contrato de concorrência da tela: a seleção é
    uma fotografia e pode envelhecer enquanto outro contador processa o NEO.
    Também garante que a decisão `sem_regra` seja encerrada pelo fluxo comum
    do motor, sem deixar a linha órfã que já ocorreu em produção.
    """
    csrf = await _login(client, tenant, usuario)
    _, conta_id = await _setup_base(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    transacoes = (
        await db.execute(
            select(Transacao)
            .where(Transacao.empresa_id == empresa.id)
            .order_by(Transacao.historico)
        )
    ).scalars().all()
    atual, velha = transacoes[:2]
    velha.status = "processada"
    await db.flush()

    resposta = await client.post(
        _pendencias_url(empresa.id, "classificar-lote"),
        json={
            "transacao_ids": [str(atual.id), str(velha.id)],
            "conta_id": conta_id,
            "descricao": "Classificação coletiva",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert resposta.status_code == 200
    assert resposta.json() == {
        "classificadas": 1,
        "ignoradas": 1,
        "ids_ignorados": [str(velha.id)],
    }
    await db.refresh(atual)
    assert atual.status == "processada"
    decisao = (
        await db.execute(
            select(NeoDecisao).where(NeoDecisao.transacao_id == atual.id)
        )
    ).scalar_one()
    assert (decisao.resultado, decisao.estrategia, decisao.conta_id) == (
        "associada",
        "manual",
        UUID(conta_id),
    )
    assert (
        await db.execute(
            select(AuditLog).where(
                AuditLog.acao == "neo.associacao_manual",
                AuditLog.entidade_id == str(decisao.id),
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_classificar_lote_recusa_mais_de_duzentos_ids(
    client, tenant, usuario, empresa
):
    """O teto de 200 impede que um clique mantenha locks demais até o commit.

    O teste trava o limite escolhido para que ele não seja removido por engano
    e transforme uma ação de tela numa operação irrestrita sobre o banco.
    """
    csrf = await _login(client, tenant, usuario)
    resposta = await client.post(
        _pendencias_url(empresa.id, "classificar-lote"),
        json={
            "transacao_ids": [str(uuid4()) for _ in range(201)],
            "conta_id": str(uuid4()),
            "descricao": "Lote grande demais",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resposta.status_code == 422


@pytest.mark.asyncio
async def test_simulacao_e_motor_concordam_no_mesmo_conjunto(
    client, db, tenant, usuario, empresa
):
    """A prévia precisa atingir exatamente o que o motor classifica depois.

    Este é o contrato central da simulação: acento, pontuação e palavra extra
    não podem produzir conjuntos diferentes entre a confirmação e a execução.
    A chamada deliberadamente não envia CSRF porque a prévia é somente leitura.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)
    simulacao = await client.post(
        _pendencias_url(empresa.id, "simular-regra"),
        json={
            "historico": "TARIFA COM LIQUIDACAO",
            "dc": "D",
            "agencia_id": agencia_id,
            "conta_id": conta_id,
        },
    )
    assert simulacao.status_code == 200
    previa = simulacao.json()
    assert previa["pendencias_atingidas"]["quantidade"] == 2

    await _criar_regra(
        client,
        empresa,
        agencia_id,
        conta_id,
        "TARIFA COM LIQUIDACAO",
        "D",
        csrf,
    )
    executada = await client.post(
        _processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf}
    )
    assert (await _resultado_do_job(client, empresa.id, executada))["associadas"] == 2
    classificadas = {
        transacao.historico
        for transacao in (
            await db.execute(
                select(Transacao).where(
                    Transacao.empresa_id == empresa.id,
                    Transacao.status == "processada",
                )
            )
        ).scalars().all()
    }
    assert classificadas == set(previa["pendencias_atingidas"]["amostras"])


@pytest.mark.asyncio
async def test_simular_regra_conta_contabilizadas_e_conflito_sem_contrapartida(
    client, db, tenant, usuario, empresa
):
    """Conflito compara a conta classificada, nunca a contrapartida bancária.

    A prévia deve distinguir uma classificação já feita na conta proposta de
    outra que a contradiz; contar a segunda partida faria todas parecerem
    conflitantes e inutilizaria o aviso ao contador.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    transacoes = (
        await db.execute(
            select(Transacao)
            .where(Transacao.empresa_id == empresa.id)
            .order_by(Transacao.id)
        )
    ).scalars().all()
    conta_divergente = PlanoConta(
        empresa_id=empresa.id,
        codigo="4.1.99",
        descricao="Outra despesa",
        tipo="despesa",
    )
    db.add(conta_divergente)
    await db.flush()
    for transacao, destino in zip(
        transacoes[:2], (conta_id, str(conta_divergente.id)), strict=True
    ):
        resposta = await client.post(
            _pendencias_url(empresa.id, "classificar-lote"),
            json={
                "transacao_ids": [str(transacao.id)],
                "conta_id": destino,
                "descricao": "Tarifa revisada",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resposta.json()["classificadas"] == 1

    simulacao = (
        await client.post(
            _pendencias_url(empresa.id, "simular-regra"),
            json={
                "historico": "TARIFA",
                "dc": "D",
                "agencia_id": agencia_id,
                "conta_id": conta_id,
            },
        )
    ).json()
    assert simulacao["pendencias_atingidas"]["quantidade"] == 1
    assert simulacao["ja_contabilizadas_atingidas"]["quantidade"] == 2
    assert simulacao["conflitos"]["quantidade"] == 1
    assert simulacao["conflitos"]["amostras"][0]["conta_id"] == str(
        conta_divergente.id
    )


@pytest.mark.asyncio
async def test_criar_regra_e_aplicar_restringe_agencia_e_mes(
    client, db, tenant, usuario, empresa
):
    """Criar uma regra não autoriza reprocessar outras agências ou competências.

    O teste trava o escopo revisado no modal: só a pendência de março da agência
    da regra é resolvida, mesmo havendo históricos idênticos fora dele.
    """
    csrf = await _login(client, tenant, usuario)
    agencias = [
        AgenciaBancaria(
            empresa_id=empresa.id,
            banco_sigla="ITAU",
            agencia=f"77{indice}",
            numero=f"7700{indice}",
        )
        for indice in range(2)
    ]
    conta = PlanoConta(
        empresa_id=empresa.id,
        codigo="4.1.77",
        descricao="Tarifas do escopo",
        tipo="despesa",
    )
    db.add_all([*agencias, conta])
    await db.flush()
    transacoes = [
        Transacao(
            empresa_id=empresa.id,
            agencia_id=agencia.id,
            data=data,
            valor=Decimal("10.00"),
            historico="TARIFA ESCOPO",
            dc="D",
            hash_dedup=f"neo-escopo-{indice}",
        )
        for indice, (agencia, data) in enumerate(
            (
                (agencias[0], datetime(2024, 3, 10, tzinfo=UTC)),
                (agencias[0], datetime(2024, 4, 10, tzinfo=UTC)),
                (agencias[1], datetime(2024, 3, 10, tzinfo=UTC)),
            )
        )
    ]
    db.add_all(transacoes)
    await db.flush()

    resposta = await client.post(
        _pendencias_url(empresa.id, "criar-regra-e-aplicar"),
        json={
            "conta_id": str(conta.id),
            "agencia_id": str(agencias[0].id),
            "descricao": "Tarifa bancária",
            "historico": "TARIFA ESCOPO",
            "dc": "D",
            "tipo": "automatica",
            "manter_historico": False,
            "mes": "2024-03",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert resposta.status_code == 201
    body = resposta.json()
    assert body["regra"]["agencia_id"] == str(agencias[0].id)
    assert body["resultado"]["total_pendentes"] == 1
    assert body["resultado"]["associadas"] == 1
    await db.refresh(transacoes[0])
    await db.refresh(transacoes[1])
    await db.refresh(transacoes[2])
    assert [transacao.status for transacao in transacoes] == [
        "processada",
        "pendente",
        "pendente",
    ]


@pytest.mark.asyncio
async def test_novas_mutacoes_em_lote_exigem_csrf(client, tenant, usuario, empresa):
    """As duas ações que escrevem não podem herdar a exceção da simulação.

    O teste trava a fronteira de segurança: prévia é leitura, classificação e
    criação/aplicação continuam protegidas contra requisições forjadas.
    """
    await _login(client, tenant, usuario)
    lote = await client.post(
        _pendencias_url(empresa.id, "classificar-lote"),
        json={
            "transacao_ids": [str(uuid4())],
            "conta_id": str(uuid4()),
            "descricao": "Tentativa sem token",
        },
    )
    criar = await client.post(
        _pendencias_url(empresa.id, "criar-regra-e-aplicar"),
        json={
            "conta_id": str(uuid4()),
            "agencia_id": str(uuid4()),
            "descricao": "Tentativa sem token",
            "historico": "TARIFA TESTE",
            "dc": "D",
        },
    )
    assert lote.status_code == 403
    assert criar.status_code == 403


@pytest.mark.asyncio
async def test_pendencias_respeita_granularidade_de_tokens(
    client, db, tenant, usuario, empresa
):
    """A tela deve controlar a granularidade: duas TEDs equivalentes com dois
    tokens precisam se separar quando o terceiro token passa a contar.
    """
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(
        db,
        empresa,
        [
            {"historico": "TED RECEBIDA CLIENTE ALFA", "dc": "C"},
            {"historico": "TED RECEBIDA EMPRESA XYZ", "dc": "C"},
        ],
    )

    com_dois = (
        await client.get(_pendencias_agrupadas_url(empresa.id), params={"tokens": 2})
    ).json()
    com_tres = (
        await client.get(_pendencias_agrupadas_url(empresa.id), params={"tokens": 3})
    ).json()

    assert com_dois["total_grupos"] == 1
    assert com_dois["grupos"][0]["padrao"] == "ted recebida"
    assert com_dois["grupos"][0]["quantidade"] == 2
    assert com_tres["total_grupos"] == 2
    assert {grupo["padrao"] for grupo in com_tres["grupos"]} == {
        "ted recebida cliente",
        "ted recebida empresa",
    }


@pytest.mark.asyncio
async def test_pendencias_ignora_decisao_orfa_de_transacao_contabilizada(
    client, db, tenant, usuario, empresa
):
    """Uma decisão antiga `sem_regra` não pode poluir a fila depois que sua
    transação foi contabilizada por outro fluxo.
    """
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(
        db,
        empresa,
        [
            {"historico": "BOLETO AINDA PENDENTE"},
            {"historico": "BOLETO JÁ CONTABILIZADO", "status": "processada"},
        ],
    )

    body = (await client.get(_pendencias_agrupadas_url(empresa.id))).json()
    assert body["total_pendentes"] == 1
    assert body["total_agrupadas"] == 1
    assert body["total_grupos"] == 1
    assert body["grupos"][0]["rotulo"] == "BOLETO AINDA PENDENTE"


@pytest.mark.asyncio
async def test_pendencias_rotulo_preserva_historico_real_mais_frequente(
    client, db, tenant, usuario, empresa
):
    """O rótulo deve manter acento e caixa do histórico mais comum para que o
    contador reconheça o extrato, sem expor a chave técnica normalizada.
    """
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(
        db,
        empresa,
        [
            {"historico": "TARIFA COM LIQUIDAÇÃO"},
            {"historico": "TARIFA COM LIQUIDAÇÃO"},
            {"historico": "Tarifa com R liquidacao"},
        ],
    )

    body = (await client.get(_pendencias_agrupadas_url(empresa.id))).json()
    grupo = body["grupos"][0]
    assert grupo["padrao"] == "tarifa com liquidacao"
    assert grupo["rotulo"] == "TARIFA COM LIQUIDAÇÃO"
    assert grupo["rotulo"] != grupo["padrao"]
    assert grupo["amostras"] == [
        "TARIFA COM LIQUIDAÇÃO",
        "Tarifa com R liquidacao",
    ]


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
    body = await _resultado_do_job(client, empresa.id, r)
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
    body_marco = await _resultado_do_job(client, empresa.id, r_marco)
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
    assert (await _resultado_do_job(client, empresa.id, r_abril))["associadas"] == 1

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
    assert (await _resultado_do_job(client, empresa.id, r))["associadas"] >= 1


@pytest.mark.asyncio
async def test_historico_iniciado_pela_regra_continua_sendo_substring(
    client, db, tenant, usuario, empresa
):
    """Começar pelo texto da regra não cria uma estratégia distinta.

    `prefixo` era inalcançável porque esse mesmo histórico sempre casa antes
    por substring. O teste impede que o bloco redundante volte antes de
    `substring` e passe a gravar uma estratégia que nunca existiu de fato.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "BOLETO", "D", csrf)

    resposta = await client.post(
        _processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resposta.status_code == 202

    decisoes = (
        await db.execute(
            select(NeoDecisao)
            .join(Transacao, Transacao.id == NeoDecisao.transacao_id)
            .where(Transacao.historico == "BOLETO ENERGIA ELETRICA")
        )
    ).scalars().all()
    assert len(decisoes) == 1
    assert decisoes[0].estrategia == "substring"


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
    assert r.status_code == 202

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
    resultado = await _resultado_do_job(client, empresa.id, r)
    assert resultado["sem_regra"] >= 1
    assert resultado["associadas"] == 0


@pytest.mark.asyncio
async def test_regra_manual_nao_classifica_transacao_no_neo(
    client, db, tenant, usuario, empresa
):
    """Uma regra `manual` aceita pela API não pode agir como automática.

    O cadastro historicamente aceitava esse tipo sem avisar que o motor o
    ignora. O teste documenta a armadilha e impede que uma mudança na carga de
    regras passe a classificar silenciosamente dados legados como manuais.
    """
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    criada = await client.post(
        f"/api/v1/empresas/{empresa.id}/regras",
        json={
            "conta_id": conta_id,
            "agencia_id": agencia_id,
            "descricao": "Boleto manual legado",
            "historico": "BOLETO ENERGIA ELETRICA",
            "dc": "D",
            "tipo": "manual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert criada.status_code == 201

    resposta = await client.post(
        _processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resposta.status_code == 202

    transacao = (
        await db.execute(
            select(Transacao).where(
                Transacao.empresa_id == empresa.id,
                Transacao.historico == "BOLETO ENERGIA ELETRICA",
            )
        )
    ).scalar_one()
    decisao = (
        await db.execute(
            select(NeoDecisao).where(NeoDecisao.transacao_id == transacao.id)
        )
    ).scalar_one()
    assert transacao.status == "pendente"
    assert decisao.resultado == "sem_regra"
    assert decisao.regra_id is None


@pytest.mark.asyncio
async def test_neo_idempotente(client, db, tenant, usuario, empresa):
    """Transações já processadas não são reprocessadas numa segunda execução."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_base(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "TED RECEBIDA CLIENTE ALFA", "C", csrf)

    r1 = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    r2 = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    resultado1 = await _resultado_do_job(client, empresa.id, r1)
    resultado2 = await _resultado_do_job(client, empresa.id, r2)

    # A transação que foi associada na 1ª execução não gera nova associação na 2ª.
    # (sem_regra permanecem pendentes para poder ser recuperadas quando novas regras forem criadas.)
    assert resultado1["associadas"] >= 1
    assert resultado2["associadas"] == 0


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
    assert resposta.status_code == 202

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
        assert resposta.status_code == 202

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


async def _criar_medicao_shadow(
    db,
    empresa,
    agencia,
    conta_regra,
    contraparte,
    *,
    valor: str,
    data: datetime,
    divergente: bool | None,
    sufixo: str,
) -> NeoDecisao:
    """Monta uma medição persistida para os testes isolarem a consulta agregada."""
    transacao = Transacao(
        empresa_id=empresa.id,
        agencia_id=agencia.id,
        data=data,
        valor=Decimal(valor),
        historico=f"CASO SHADOW {sufixo}",
        dc="D",
        status="processada",
        hash_dedup=f"shadow-relatorio-{sufixo}",
    )
    db.add(transacao)
    await db.flush()
    decisao = NeoDecisao(
        empresa_id=empresa.id,
        transacao_id=transacao.id,
        conta_id=conta_regra.id,
        resultado="associada",
        estrategia="substring",
        contraparte_id=contraparte.id if divergente is not None else None,
        conta_contraparte_id=(
            contraparte.conta_contabil_id if divergente is not None else None
        ),
        origem_evidencia="nota_fiscal" if divergente is not None else None,
        conta_divergente=divergente,
    )
    db.add(decisao)
    await db.flush()
    return decisao


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
    conta, o lançamento continua na conta da regra e a divergência fica na
    mesma decisão — trava que shadow mode só observa e nunca classifica."""
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
    contraparte = await _criar_contraparte(
        db, empresa, conta_contraparte, documento="52540787000188"
    )
    await _criar_regra(client, empresa, agencia_id, conta_regra, "BOLETO", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 202

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

    decisao = (
        await db.execute(select(NeoDecisao).where(NeoDecisao.transacao_id == debito.id))
    ).scalar_one()
    assert decisao.conta_id == UUID(conta_regra)
    assert decisao.contraparte_id == contraparte.id
    assert decisao.conta_contraparte_id == conta_contraparte.id
    assert decisao.origem_evidencia == "nota_fiscal"
    assert decisao.conta_divergente is True


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
    body = await _resultado_do_job(client, empresa.id, r)
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
    resultado = await _resultado_do_job(client, empresa.id, r)
    assert resultado["classificadas_por_contraparte"] == 0

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
    resultado = await _resultado_do_job(client, empresa.id, r)
    assert resultado["sem_regra"] >= 1


@pytest.mark.asyncio
async def test_divergencias_agrega_valor_ordena_pares_e_respeita_filtros(
    client, db, tenant, usuario, empresa
):
    """O relatório precisa contar FALSE como avaliado, ignorar NULL e priorizar
    os pares de maior valor; trava também que mês e agência recortam todos os
    números, pois misturar medições fora do filtro mudaria a decisão de produto."""
    await _login(client, tenant, usuario)
    agencias = [
        AgenciaBancaria(
            empresa_id=empresa.id,
            banco_sigla="ITAU",
            agencia=f"000{indice}",
            numero=f"1000{indice}",
        )
        for indice in (1, 2)
    ]
    contas = [
        PlanoConta(
            empresa_id=empresa.id,
            codigo=f"4.8.{indice}",
            descricao=f"Conta relatório {indice}",
            tipo="despesa",
        )
        for indice in range(1, 5)
    ]
    db.add_all([*agencias, *contas])
    await db.flush()
    contraparte_maior = await _criar_contraparte(
        db, empresa, contas[1], documento="11111111000111"
    )
    contraparte_menor = await _criar_contraparte(
        db, empresa, contas[3], documento="22222222000122"
    )
    contraparte_concordante = await _criar_contraparte(
        db, empresa, contas[0], documento="33333333000133"
    )

    marco = datetime(2024, 3, 15, tzinfo=UTC)
    await _criar_medicao_shadow(
        db, empresa, agencias[0], contas[0], contraparte_maior,
        valor="50000.00", data=marco, divergente=True, sufixo="maior",
    )
    await _criar_medicao_shadow(
        db, empresa, agencias[0], contas[0], contraparte_concordante,
        valor="20.00", data=marco, divergente=False, sufixo="concordante",
    )
    await _criar_medicao_shadow(
        db, empresa, agencias[0], contas[2], contraparte_menor,
        valor="20.00", data=marco, divergente=True, sufixo="menor",
    )
    # Ausência de medição não é concordância, mesmo com valor muito alto.
    await _criar_medicao_shadow(
        db, empresa, agencias[0], contas[0], contraparte_maior,
        valor="99999.00", data=marco, divergente=None, sufixo="nao-avaliado",
    )
    await _criar_medicao_shadow(
        db, empresa, agencias[1], contas[0], contraparte_maior,
        valor="1000.00", data=marco, divergente=True, sufixo="outra-agencia",
    )
    await _criar_medicao_shadow(
        db, empresa, agencias[0], contas[0], contraparte_maior,
        valor="300.00", data=datetime(2024, 4, 1, tzinfo=UTC),
        divergente=True, sufixo="outro-mes",
    )

    resposta = await client.get(
        f"/api/v1/empresas/{empresa.id}/neo/divergencias"
        f"?mes=2024-03&agencia_id={agencias[0].id}"
    )

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["total_avaliadas"] == 3
    assert body["total_divergentes"] == 2
    assert body["percentual_divergentes"] == 66.67
    assert Decimal(body["valor_total_divergente"]) == Decimal("50020.00")
    assert [Decimal(item["valor_total"]) for item in body["por_conta"]] == [
        Decimal("50000.00"),
        Decimal("20.00"),
    ]
    assert [item["quantidade"] for item in body["por_conta"]] == [1, 1]
    assert [item["historico"] for item in body["amostra"]] == [
        "CASO SHADOW maior",
        "CASO SHADOW menor",
    ]
    assert body["amostra"][0]["conta_regra_id"] == str(contas[0].id)
    assert body["amostra"][0]["conta_contraparte_id"] == str(contas[1].id)


@pytest.mark.asyncio
async def test_divergencias_sem_medicao_retorna_agregado_vazio(
    client, db, tenant, usuario, empresa
):
    """Sem shadow avaliado o endpoint deve devolver zeros e listas vazias;
    trava a semântica de que ausência de medição não vira conflito nem 100%."""
    await _login(client, tenant, usuario)

    resposta = await client.get(f"/api/v1/empresas/{empresa.id}/neo/divergencias")

    assert resposta.status_code == 200
    assert resposta.json() == {
        "total_avaliadas": 0,
        "total_divergentes": 0,
        "percentual_divergentes": 0.0,
        "valor_total_divergente": "0.00",
        "por_conta": [],
        "amostra": [],
    }


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

    # Reproduz uma linha anterior à migration: o filtro ainda precisa inferir
    # a conta pela regra quando o backfill não estiver presente nessa cópia.
    decisao_legada = (
        await db.execute(
            select(NeoDecisao)
            .join(Transacao, Transacao.id == NeoDecisao.transacao_id)
            .where(Transacao.historico == "TED RECEBIDA CLIENTE ALFA")
        )
    ).scalar_one()
    decisao_legada.conta_id = None
    await db.flush()

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
async def test_filtro_conta_encontra_decisoes_manual_e_por_contraparte(
    client, db, tenant, usuario, empresa
):
    """O filtro deve usar a conta aplicada mesmo quando não existe regra.

    Antes, o filtro comparava apenas `Regra.conta_id`: decisões manuais e por
    contraparte têm `regra_id` nulo e desapareciam, embora seus lançamentos
    estivessem contabilizados na conta pesquisada.
    """
    csrf = await _login(client, tenant, usuario)
    _, conta_id = await _setup_base(client, db, empresa, csrf)
    conta = (
        await db.execute(select(PlanoConta).where(PlanoConta.id == UUID(conta_id)))
    ).scalar_one()
    transacoes = {
        transacao.historico: transacao
        for transacao in (
            await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
        ).scalars().all()
    }
    deposito = transacoes["DEPOSITO AVULSO DESCONHECIDO"]
    db.add(
        NotaFiscal(
            empresa_id=empresa.id,
            tipo="nfe",
            numero="NF-FILTRO-CONTA",
            cnpj_emitente=empresa.cnpj,
            cnpj_destinatario="52.540.787/0001-88",
            valor=deposito.valor,
            data_emissao=deposito.data,
            dedup_key="teste-filtro-conta-contraparte",
        )
    )
    await _criar_contraparte(
        db,
        empresa,
        conta,
        documento="52540787000188",
        tipo="cliente",
        razao_social="Cliente do filtro",
    )
    await db.flush()

    processada = await client.post(
        _processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf}
    )
    resultado = await _resultado_do_job(client, empresa.id, processada)
    assert resultado["classificadas_por_contraparte"] == 1

    sem_regra = (
        await client.get(_decisoes_url(empresa.id) + "?resultado=sem_regra")
    ).json()["items"]
    boleto = next(
        item for item in sem_regra
        if item["transacao_descricao"] == "BOLETO ENERGIA ELETRICA"
    )
    associada = await client.post(
        _decisoes_url(empresa.id) + f"/{boleto['id']}/associar-manual",
        json={"conta_id": conta_id, "descricao": "Boleto classificado à mão"},
        headers={"X-CSRF-Token": csrf},
    )
    assert associada.status_code == 200
    assert associada.json()["conta_id"] == conta_id

    filtradas = (
        await client.get(_decisoes_url(empresa.id) + f"?conta_id={conta_id}")
    ).json()["items"]
    por_estrategia = {item["estrategia"]: item for item in filtradas}
    assert {"manual", "contraparte"} <= por_estrategia.keys()
    for estrategia in ("manual", "contraparte"):
        assert por_estrategia[estrategia]["conta_id"] == conta_id
        assert por_estrategia[estrategia]["conta_codigo"] == conta.codigo
        assert por_estrategia[estrategia]["conta_descricao"] == conta.descricao


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
    assert r.status_code == 202
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
    resultado = await _resultado_do_job(client, empresa.id, r)
    assert resultado["associadas"] == 3
    assert resultado["sem_regra"] == 0


@pytest.mark.asyncio
async def test_regra_com_acento_casa_historico_sem_acento(client, db, tenant, usuario, empresa):
    """A regra é digitada com acento e o extrato vem sem — e vice-versa."""
    csrf = await _login(client, tenant, usuario)
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)

    await _criar_regra(client, empresa, agencia_id, conta_id, "TARIFA COM LIQUIDAÇÃO", "D", csrf)

    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 202

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
    assert r.status_code == 202

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
    assert r.status_code == 202

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
    assert (await _resultado_do_job(client, empresa.id, r))["sem_regra"] == 3

    sem_regra = (await client.get(_decisoes_url(empresa.id) + "?resultado=sem_regra")).json()
    assert sem_regra["total"] == 3

    await _criar_regra(client, empresa, agencia_id, conta_id, "TARIFA", "D", csrf)
    r = await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})
    assert (await _resultado_do_job(client, empresa.id, r))["associadas"] == 3

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


# ── Filtros novos da fila de decisões ────────────────────────────────────────


async def _decisoes(client, empresa, **params) -> dict:
    from urllib.parse import urlencode

    r = await client.get(
        f"/api/v1/empresas/{empresa.id}/neo/decisoes?{urlencode(params)}"
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_decisao_traz_a_data_do_lancamento(client, db, tenant, usuario, empresa):
    """A fila mostra "Data | Histórico | Valor".

    Sem a data na resposta, a tela precisaria buscar transação por transação —
    um N+1 para exibir uma coluna.
    """
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(
        db, empresa, [{"historico": "TARIFA MENSAL", "data": date(2026, 5, 7)}]
    )

    body = await _decisoes(client, empresa)

    assert body["items"][0]["transacao_data"] == "2026-05-07"


@pytest.mark.asyncio
async def test_filtra_decisoes_por_intervalo_de_datas(
    client, db, tenant, usuario, empresa
):
    """Intervalo livre, inclusivo nas duas pontas — não só competência fechada."""
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(db, empresa, [
        {"historico": "ANTES", "data": date(2026, 5, 1)},
        {"historico": "DENTRO", "data": date(2026, 5, 15)},
        {"historico": "ULTIMO DIA", "data": date(2026, 5, 31)},
        {"historico": "DEPOIS", "data": date(2026, 6, 1)},
    ])

    body = await _decisoes(client, empresa, data_de="2026-05-15", data_ate="2026-05-31")

    historicos = {i["transacao_descricao"] for i in body["items"]}
    assert historicos == {"DENTRO", "ULTIMO DIA"}


@pytest.mark.asyncio
async def test_intervalo_invertido_responde_422(client, db, tenant, usuario, empresa):
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(db, empresa, [{"historico": "QUALQUER"}])

    from urllib.parse import urlencode

    r = await client.get(
        f"/api/v1/empresas/{empresa.id}/neo/decisoes?"
        + urlencode({"data_de": "2026-05-31", "data_ate": "2026-05-01"})
    )

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_intervalo_recorta_dentro_da_competencia(
    client, db, tenant, usuario, empresa
):
    """Competência e intervalo se acumulam — o intervalo é recorte, não disputa.

    A competência é global na aplicação; um intervalo informado na tela é mais
    específico e não pode ser descartado por ela.
    """
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(db, empresa, [
        {"historico": "INICIO DO MES", "data": date(2026, 5, 2)},
        {"historico": "MEIO DO MES", "data": date(2026, 5, 20)},
        {"historico": "OUTRO MES", "data": date(2026, 6, 20)},
    ])

    body = await _decisoes(
        client, empresa, mes="2026-05", data_de="2026-05-15", data_ate="2026-05-31"
    )

    assert {i["transacao_descricao"] for i in body["items"]} == {"MEIO DO MES"}


@pytest.mark.asyncio
async def test_competencia_e_intervalo_incompativeis_devolvem_vazio(
    client, db, tenant, usuario, empresa
):
    """Pedir junho e um intervalo de maio é contraditório — vazio é honesto."""
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(db, empresa, [
        {"historico": "JUNHO", "data": date(2026, 6, 10)},
    ])

    body = await _decisoes(
        client, empresa, mes="2026-06", data_de="2026-05-01", data_ate="2026-05-31"
    )

    assert body["total"] == 0


@pytest.mark.asyncio
async def test_filtra_decisoes_por_motivo(client, db, tenant, usuario, empresa):
    """O motivo é o que explica por que o lançamento parou na fila."""
    await _login(client, tenant, usuario)
    await _registrar_fila_agrupada(db, empresa, [{"historico": "PIX ENVIADO"}])

    achou = await _decisoes(client, empresa, motivo="agrupamento")
    nao_achou = await _decisoes(client, empresa, motivo="inexistente")

    assert achou["total"] == 1
    assert nao_achou["total"] == 0


# ── Cancelamento de lançamento (fase 01 do estorno auditado) ─────────────────


async def _classificar_uma(client, db, empresa, csrf) -> tuple[str, str]:
    """Deixa uma transação contabilizada e devolve (lancamento_id, transacao_id)."""
    agencia_id, conta_id = await _setup_tarifas(client, db, empresa, csrf)
    await _criar_regra(client, empresa, agencia_id, conta_id, "TARIFA COM LIQUIDACAO", "D", csrf)
    await client.post(_processar_url(empresa.id), json={}, headers={"X-CSRF-Token": csrf})

    registro = (
        await db.execute(
            select(RegistroContabil).where(
                RegistroContabil.empresa_id == empresa.id,
                RegistroContabil.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    assert registro is not None, "o motor não contabilizou nada"
    return str(registro.lancamento_id), str(registro.transacao_id)


def _cancelar_url(empresa_id, lancamento_id) -> str:
    return f"/api/v1/empresas/{empresa_id}/neo/lancamentos/{lancamento_id}/cancelar"


@pytest.mark.asyncio
async def test_cancelar_apaga_o_par_e_devolve_a_transacao_para_a_fila(
    client, db, tenant, usuario, empresa
):
    """As duas partidas somem juntas e a transação volta a ser classificável."""
    csrf = await _login(client, tenant, usuario)
    lancamento_id, transacao_id = await _classificar_uma(client, db, empresa, csrf)

    r = await client.post(
        _cancelar_url(empresa.id, lancamento_id),
        json={"motivo": "conta errada"},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 200, r.text
    assert r.json()["partidas_canceladas"] == 2

    ativas = (
        await db.execute(
            select(RegistroContabil).where(
                RegistroContabil.lancamento_id == UUID(lancamento_id),
                RegistroContabil.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    assert ativas == []

    transacao = (
        await db.execute(select(Transacao).where(Transacao.id == UUID(transacao_id)))
    ).scalar_one()
    await db.refresh(transacao)
    assert transacao.status == "pendente"


@pytest.mark.asyncio
async def test_cancelar_duas_vezes_devolve_404_e_nao_500(
    client, db, tenant, usuario, empresa
):
    """Clicar duas vezes é cenário real — precisa ser informação, não erro."""
    csrf = await _login(client, tenant, usuario)
    lancamento_id, _ = await _classificar_uma(client, db, empresa, csrf)
    corpo = {"motivo": "conta errada"}

    primeira = await client.post(
        _cancelar_url(empresa.id, lancamento_id), json=corpo,
        headers={"X-CSRF-Token": csrf},
    )
    segunda = await client.post(
        _cancelar_url(empresa.id, lancamento_id), json=corpo,
        headers={"X-CSRF-Token": csrf},
    )

    assert primeira.status_code == 200
    assert segunda.status_code == 404


@pytest.mark.asyncio
async def test_cancelar_exige_motivo(client, db, tenant, usuario, empresa):
    """Sem motivo a trilha de auditoria vira lista de carimbos."""
    csrf = await _login(client, tenant, usuario)
    lancamento_id, _ = await _classificar_uma(client, db, empresa, csrf)

    r = await client.post(
        _cancelar_url(empresa.id, lancamento_id),
        json={"motivo": "  "},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_transacao_cancelada_pode_ser_classificada_de_novo(
    client, db, tenant, usuario, empresa
):
    """O ponto da fase 01: reclassificar deixa de ser um beco sem saída.

    `associar_manual` recusa transação `processada`. Depois do cancelamento ela
    volta a `pendente` e aceita a classificação nova.
    """
    csrf = await _login(client, tenant, usuario)
    lancamento_id, transacao_id = await _classificar_uma(client, db, empresa, csrf)
    await client.post(
        _cancelar_url(empresa.id, lancamento_id),
        json={"motivo": "reclassificar"},
        headers={"X-CSRF-Token": csrf},
    )

    outra_conta = PlanoConta(
        empresa_id=empresa.id, codigo="4.1.8", descricao="Despesas Financeiras", tipo="despesa"
    )
    db.add(outra_conta)
    await db.flush()
    decisao_id = (
        await db.execute(
            select(NeoDecisao)
            .where(NeoDecisao.transacao_id == UUID(transacao_id))
            .order_by(NeoDecisao.processado_em.desc())
        )
    ).scalars().first().id

    r = await client.post(
        _decisoes_url(empresa.id) + f"/{decisao_id}/associar-manual",
        json={"conta_id": str(outra_conta.id), "descricao": "Tarifa reclassificada"},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_cancelamento_fica_na_trilha_de_auditoria(
    client, db, tenant, usuario, empresa
):
    """Quem desfez, o que havia antes e por quê."""
    from src.db.models import AuditLog

    csrf = await _login(client, tenant, usuario)
    lancamento_id, _ = await _classificar_uma(client, db, empresa, csrf)

    await client.post(
        _cancelar_url(empresa.id, lancamento_id),
        json={"motivo": "lancado na conta errada"},
        headers={"X-CSRF-Token": csrf},
    )

    log = (
        await db.execute(
            select(AuditLog).where(AuditLog.acao == "lancamento.cancelado")
        )
    ).scalars().first()
    assert log is not None
    assert log.entidade_id == lancamento_id
    assert log.usuario_id == usuario.id
    assert "lancado na conta errada" in (log.dados_depois or "")
    # O que havia antes precisa estar guardado — é o que permite reconstruir.
    assert "partidas" in (log.dados_antes or "")
