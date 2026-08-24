"""Testes de integração — Importação de Extrato OFX."""

from __future__ import annotations

import io
import json

import pytest
from httpx import AsyncClient

from decimal import Decimal

from sqlalchemy import select

from src.db.models import Empresa, Tenant, Transacao, Usuario

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
    if r.status_code == 202:
        job = (
            await client.get(
                f"/api/v1/empresas/{empresa.id}/jobs/{r.json()['id']}"
            )
        ).json()
        # Os testes abaixo exercitam deduplicação/validação do domínio. O teste
        # específico de jobs valida separadamente o envelope persistente.
        if job["resultado"] is not None:
            r._content = json.dumps(job["resultado"]).encode()
    return r


# ── Importação


@pytest.mark.asyncio
async def test_importar_ofx_sucesso(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    r = await _importar(client, empresa, agencia["id"], csrf)
    assert r.status_code == 202
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
    assert r1.status_code == 202
    assert r1.json()["importadas"] == 2

    r2 = await _importar(client, empresa, agencia["id"], csrf)
    assert r2.status_code == 202
    body2 = r2.json()
    assert body2["importadas"] == 0
    assert body2["duplicadas"] == 2


@pytest.mark.asyncio
async def test_fitid_repetido_no_mesmo_lote_e_deduplicado(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    repetido = _OFX_VALIDO.replace("<FITID>TX002", "<FITID>TX001")

    r = await _importar(client, empresa, agencia["id"], csrf, repetido)

    assert r.status_code == 202
    assert r.json()["total_no_arquivo"] == 2
    assert r.json()["importadas"] == 1
    assert r.json()["duplicadas"] == 1


@pytest.mark.asyncio
async def test_bloco_ofx_rejeitado_aparece_na_contagem_de_erros(client, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    invalido = "<STMTTRN><DTPOSTED>20240117<TRNAMT>10.00</STMTTRN>"
    conteudo = _OFX_VALIDO.replace("</BANKTRANLIST>", invalido + "</BANKTRANLIST>")

    r = await _importar(client, empresa, agencia["id"], csrf, conteudo)

    assert r.status_code == 202
    assert r.json()["total_no_arquivo"] == 3
    assert r.json()["importadas"] == 2
    assert r.json()["erros"] == 1


@pytest.mark.asyncio
async def test_importar_ofx_invalido_rejeita(client, tenant, usuario, empresa):
    """Arquivo inválido falha no job para a requisição não voltar a ser síncrona."""
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    conteudo_invalido = "isso nao e um arquivo ofx valido !!!"
    r = await _importar(client, empresa, agencia["id"], csrf, conteudo=conteudo_invalido)
    assert r.status_code == 202
    job = await client.get(
        f"/api/v1/empresas/{empresa.id}/jobs/{r.json()['id']}"
    )
    assert job.json()["status"] == "falhou"
    assert job.json()["erro"]


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


# ── Valor não confiável não entra no banco ───────────────────────────────────
#
# Reproduz o caso real da SINCOPEÇAS (agosto/2026): extrato em PDF caiu na
# camada de IA do parser, que devolveu a linha inteira como descrição e
# capturou a coluna de saldo no lugar do valor. Vinte e nove transações
# gravadas com o saldo da conta — uma tarifa de R$ 1,19 virou R$ 54.881,83 a
# crédito. Aqui o vetor é OFX porque o teste não precisa do parser de PDF para
# provar a barreira: o que importa é que a transação não seja persistida.

_OFX_LINHA_CRUA = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260218
<TRNAMT>54881.83
<FITID>TX_SALDO_COMO_VALOR
<MEMO>18/02/2026 TARIFA COM R LIQUIDACAO COB000001 -1,19 -54.881,83
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260219
<TRNAMT>-350.00
<FITID>TX_LIMPA
<MEMO>PIX ENVIADO MARIA SILVA
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


@pytest.mark.asyncio
async def test_transacao_com_saldo_no_lugar_do_valor_nao_e_importada(
    client, db, tenant, usuario, empresa
):
    """A linha ruim é recusada e a boa passa — recusar o arquivo inteiro por
    causa de uma linha seria pior, porque quebra a importação de extratos que
    são majoritariamente válidos.

    A transação errada não pode chegar ao banco: valor errado entra em
    silêncio e contamina o razão, enquanto a linha faltando aparece na
    conciliação e pode ser corrigida à mão.
    """
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    r = await _importar(client, empresa, agencia["id"], csrf, conteudo=_OFX_LINHA_CRUA)
    assert r.status_code == 202
    body = r.json()

    assert body["importadas"] == 1
    assert body["rejeitadas"] == 1
    assert len(body["motivos_rejeicao"]) == 1
    # A mensagem diz qual era o valor certo: é o que permite lançar à mão sem
    # reabrir o PDF do extrato.
    assert "R$ 1,19" in body["motivos_rejeicao"][0]

    persistidas = (
        await db.execute(select(Transacao).where(Transacao.empresa_id == empresa.id))
    ).scalars().all()
    assert [t.historico for t in persistidas] == ["PIX ENVIADO MARIA SILVA"]
    assert all(t.valor != Decimal("54881.83") for t in persistidas)


@pytest.mark.asyncio
async def test_extrato_normal_nao_ganha_rejeicao(client, tenant, usuario, empresa):
    """A barreira não pode cobrar pedágio de quem está certo: extrato bem
    parseado tem descrição limpa e não entra na checagem."""
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    body = (await _importar(client, empresa, agencia["id"], csrf)).json()

    assert body["importadas"] == 2
    assert body["rejeitadas"] == 0
    assert body["motivos_rejeicao"] == []


# ── Filtros da listagem ──────────────────────────────────────────────────────


async def _semear(db, empresa, agencia_id) -> None:
    """Três lançamentos com histórico, valor, D/C e data distintos."""
    from datetime import date
    from uuid import UUID

    for i, (dia, hist, valor, dc) in enumerate(
        [
            (date(2026, 3, 1), "PAGAMENTO PIX FORNECEDOR ALFA", Decimal("100.00"), "D"),
            (date(2026, 3, 15), "TARIFA COM R LIQUIDACAO", Decimal("1.19"), "D"),
            (date(2026, 3, 31), "TED RECEBIDA CLIENTE BETA", Decimal("5000.00"), "C"),
        ]
    ):
        db.add(
            Transacao(
                empresa_id=empresa.id,
                agencia_id=UUID(agencia_id),
                data=dia,
                valor=valor,
                historico=hist,
                dc=dc,
                hash_dedup=f"hash_filtro_{i}",
            )
        )
    await db.flush()


async def _listar(client, empresa, **params) -> dict:
    r = await client.get(f"/api/v1/empresas/{empresa.id}/extrato", params=params)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_filtra_por_trecho_do_historico(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _semear(db, empresa, agencia["id"])

    body = await _listar(client, empresa, historico="tarifa")

    assert body["total"] == 1
    assert "TARIFA" in body["items"][0]["historico"]


@pytest.mark.asyncio
async def test_filtra_por_faixa_de_valor(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _semear(db, empresa, agencia["id"])

    body = await _listar(client, empresa, valor_min=50, valor_max=1000)

    assert [Decimal(str(i["valor"])) for i in body["items"]] == [Decimal("100.00")]


@pytest.mark.asyncio
async def test_filtra_por_debito_ou_credito(client, db, tenant, usuario, empresa):
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _semear(db, empresa, agencia["id"])

    body = await _listar(client, empresa, dc="C")

    assert body["total"] == 1
    assert body["items"][0]["dc"] == "C"


@pytest.mark.asyncio
async def test_periodo_inclui_o_ultimo_dia(client, db, tenant, usuario, empresa):
    """31/03 tem de entrar quando `data_ate=2026-03-31`.

    Enquanto o filtro era datetime, a data virava 00:00 e o último dia inteiro
    ficava de fora — o lançamento do dia 31 sumia do extrato.
    """
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _semear(db, empresa, agencia["id"])

    body = await _listar(client, empresa, data_de="2026-03-01", data_ate="2026-03-31")

    assert body["total"] == 3
    assert "2026-03-31" in {i["data"] for i in body["items"]}


@pytest.mark.asyncio
async def test_data_sai_como_dia_sem_fuso(client, db, tenant, usuario, empresa):
    """`2026-03-01`, não `2026-03-01T00:00:00Z` — que o front renderizava como 28/02."""
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _semear(db, empresa, agencia["id"])

    body = await _listar(client, empresa, historico="ALFA")

    assert body["items"][0]["data"] == "2026-03-01"


@pytest.mark.asyncio
async def test_transacao_apagada_some_da_listagem(client, db, tenant, usuario, empresa):
    """Soft delete precisa esconder de fato — é o que a limpeza de extrato usa."""
    from datetime import UTC, datetime

    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _semear(db, empresa, agencia["id"])
    antes = await _listar(client, empresa)

    alvo = (await db.execute(select(Transacao).limit(1))).scalars().first()
    alvo.deleted_at = datetime.now(UTC)
    await db.flush()

    depois = await _listar(client, empresa)

    assert depois["total"] == antes["total"] - 1
    assert str(alvo.id) not in {i["id"] for i in depois["items"]}


@pytest.mark.asyncio
async def test_lista_da_data_mais_antiga_para_a_mais_recente(
    client, db, tenant, usuario, empresa
):
    """Extrato se lê como o papel do banco: do mais antigo para o mais recente."""
    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    await _semear(db, empresa, agencia["id"])

    body = await _listar(client, empresa)

    assert [i["data"] for i in body["items"]] == [
        "2026-03-01",
        "2026-03-15",
        "2026-03-31",
    ]


@pytest.mark.asyncio
async def test_paginacao_nao_repete_nem_pula_no_mesmo_dia(
    client, db, tenant, usuario, empresa
):
    """Vários lançamentos no mesmo dia precisam de desempate estável.

    `data` é só um dia; sem `id` como critério secundário o banco pode devolver
    ordens diferentes entre páginas, repetindo e pulando linhas.
    """
    from datetime import date
    from uuid import UUID

    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)
    for i in range(10):
        db.add(
            Transacao(
                empresa_id=empresa.id,
                agencia_id=UUID(agencia["id"]),
                data=date(2026, 5, 4),          # todos no MESMO dia
                valor=Decimal("10.00"),
                historico=f"LANCAMENTO {i}",
                dc="D",
                hash_dedup=f"hash_pag_{i}",
            )
        )
    await db.flush()

    p1 = await _listar(client, empresa, page=1, page_size=4)
    p2 = await _listar(client, empresa, page=2, page_size=4)
    p3 = await _listar(client, empresa, page=3, page_size=4)

    vistos = [i["id"] for i in p1["items"] + p2["items"] + p3["items"]]
    assert len(vistos) == len(set(vistos)) == 10


@pytest.mark.asyncio
async def test_mesmo_dia_segue_a_ordem_do_extrato(client, db, tenant, usuario, empresa):
    """Dentro do dia, a ordem é a do arquivo — não a do saldo nem a do valor.

    Caso real (SINCOPEÇAS, 02/01/2026): a tarifa de R$ 0,20 vem PRIMEIRO, com o
    maior saldo, e o pagamento de R$ 2.164,48 vem por último, com o menor. Num
    dia com crédito a relação se inverte, então nenhuma ordenação por saldo
    reproduz o extrato — só a posição da linha reproduz.
    """
    from datetime import date
    from uuid import UUID

    csrf = await _login(client, tenant, usuario)
    agencia = await _criar_agencia(client, empresa, csrf)

    # Inseridos FORA de ordem de propósito: quem manda é `ordem`, não a inserção.
    for ordem, hist, valor, saldo in [
        (2, "PAGAMENTO PIX CASSIA", Decimal("2164.48"), Decimal("58443.17")),
        (0, "TARIFA BAIXA DE TITULOS", Decimal("0.20"), Decimal("67201.27")),
        (1, "TRANSF ENTRE CONTAS CEZAR", Decimal("6593.62"), Decimal("60607.65")),
    ]:
        db.add(
            Transacao(
                empresa_id=empresa.id,
                agencia_id=UUID(agencia["id"]),
                data=date(2026, 1, 2),
                valor=valor,
                saldo_apos=saldo,
                historico=hist,
                dc="D",
                ordem=ordem,
                hash_dedup=f"hash_ordem_{ordem}",
            )
        )
    await db.flush()

    body = await _listar(client, empresa)

    assert [i["historico"] for i in body["items"]] == [
        "TARIFA BAIXA DE TITULOS",
        "TRANSF ENTRE CONTAS CEZAR",
        "PAGAMENTO PIX CASSIA",
    ]
    # O saldo NÃO está em ordem decrescente por acaso — está na ordem do banco.
    saldos = [Decimal(str(i["saldo_apos"])) for i in body["items"]]
    assert saldos == [Decimal("67201.27"), Decimal("60607.65"), Decimal("58443.17")]
