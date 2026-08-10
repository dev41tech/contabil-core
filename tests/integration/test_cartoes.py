"""Testes de integração — Cartão de Crédito (cartões, faturas, lançamentos, CSV).

O ponto sensível é a fatura paga: uma vez quitada e associada à transação bancária
que a pagou, ela não pode mais mudar de valor — senão a conciliação passa a apontar
para um total que já não existe. Boa parte destes testes é sobre isso.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    AgenciaBancaria,
    Empresa,
    FaturaCartao,
    PlanoConta,
    Tenant,
    Transacao,
    Usuario,
)


def _money(value: object) -> Decimal:
    return Decimal(str(value))


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, sufixo: str = "") -> str:
    return f"/api/v1/empresas/{empresa_id}/cartoes{sufixo}"


@pytest_asyncio.fixture
async def transacao(db: AsyncSession, empresa: Empresa) -> Transacao:
    ag = AgenciaBancaria(empresa_id=empresa.id, banco_sigla="ITAU", agencia="7285", numero="12287")
    db.add(ag)
    await db.flush()

    t = Transacao(
        empresa_id=empresa.id,
        agencia_id=ag.id,
        data=datetime(2026, 4, 10, tzinfo=UTC),
        valor=2_500,
        historico="PAGAMENTO FATURA CARTAO",
        dc="D",
        hash_dedup="hash_fatura_cartao",
    )
    db.add(t)
    await db.flush()
    return t


async def _criar_cartao(client: AsyncClient, empresa: Empresa, csrf: str, **over) -> dict:
    payload = {
        "nome": "Cartão Corporativo",
        "bandeira": "visa",
        "ultimos_digitos": "4321",
        "dia_fechamento": 20,
        "dia_vencimento": 28,
        "limite": 50_000,
    }
    payload.update(over)
    r = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text
    return r.json()


async def _criar_fatura(
    client: AsyncClient, empresa: Empresa, cartao_id: str, csrf: str, competencia="2026-03"
) -> dict:
    r = await client.post(
        _url(empresa.id, f"/{cartao_id}/faturas"),
        json={"competencia": competencia},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _add_lancamento(
    client: AsyncClient, empresa: Empresa, cartao_id: str, fatura_id: str, csrf: str, valor: float
) -> dict:
    r = await client.post(
        _url(empresa.id, f"/{cartao_id}/faturas/{fatura_id}/lancamentos"),
        json={
            "data_compra": "2026-03-05T00:00:00Z",
            "descricao": f"COMPRA {valor}",
            "valor": valor,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── acesso ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listar_sem_autenticacao_rejeita(client: AsyncClient, empresa: Empresa):
    assert (await client.get(_url(empresa.id))).status_code == 401


@pytest.mark.asyncio
async def test_criar_cartao_sem_csrf_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.post(_url(empresa.id), json={"nome": "X", "bandeira": "visa",
                                                  "dia_fechamento": 1, "dia_vencimento": 10})
    assert r.status_code == 403


# ── cartão ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_criar_e_listar_cartao(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    criado = await _criar_cartao(client, empresa, csrf)
    assert criado["bandeira"] == "visa"

    body = (await client.get(_url(empresa.id))).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == criado["id"]


@pytest.mark.asyncio
async def test_bandeira_invalida_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"nome": "Cartão", "bandeira": "bandeira_inventada",
              "dia_fechamento": 10, "dia_vencimento": 20},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_ultimos_digitos_precisa_ter_4_numeros(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"nome": "Cartão", "bandeira": "visa", "ultimos_digitos": "12ab",
              "dia_fechamento": 10, "dia_vencimento": 20},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_remover_cartao_com_fatura_em_aberto_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Remover cartão com fatura pendente esconderia dívida do balanço."""
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    await _criar_fatura(client, empresa, cartao["id"], csrf)

    r = await client.delete(_url(empresa.id, f"/{cartao['id']}"), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_remover_cartao_sem_fatura_funciona(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)

    r = await client.delete(_url(empresa.id, f"/{cartao['id']}"), headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    assert (await client.get(_url(empresa.id))).json()["total"] == 0


# ── fatura ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fatura_duplicada_na_mesma_competencia_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    await _criar_fatura(client, empresa, cartao["id"], csrf, competencia="2026-03")

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas"),
        json={"competencia": "2026-03"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_competencia_fora_do_formato_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas"),
        json={"competencia": "março/2026"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("competencia", ["2026-00", "2026-13", "2026-99"])
async def test_competencia_com_mes_invalido_rejeita(
    competencia: str,
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas"),
        json={"competencia": competencia},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_fatura_nasce_aberta_e_zerada(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    assert fatura["status"] == "aberta"
    assert _money(fatura["valor_total"]) == Decimal("0.00")
    assert fatura["total_lancamentos"] == 0


@pytest.mark.asyncio
async def test_fatura_paga_nao_reabre(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 2_500)

    await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.patch(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}"),
        json={"status": "aberta"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


# ── associação com a transação que pagou ──────────────────────────────────────


@pytest.mark.asyncio
async def test_associar_transacao_marca_como_paga(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 2_500)

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "paga"
    assert r.json()["transacao_id"] == str(transacao.id)


@pytest.mark.asyncio
async def test_desassociar_volta_para_fechada(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 2_500)

    await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )
    r = await client.delete(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "fechada"
    assert r.json()["transacao_id"] is None


@pytest.mark.asyncio
async def test_associar_transacao_inexistente_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        json={"transacao_id": str(uuid.uuid4())},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_status_paga_nao_pode_ser_definido_diretamente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    r = await client.patch(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}"),
        json={"status": "paga"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_associar_exige_mesmo_valor_e_direcao_de_debito(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    transacao: Transacao,
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 1)
    url = _url(
        empresa.id,
        f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao",
    )

    r = await client.post(
        url,
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422

    transacao.valor = 1
    transacao.dc = "C"
    await db.flush()
    r = await client.post(
        url,
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_mesma_transacao_nao_quita_duas_faturas(
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    transacao: Transacao,
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura_a = await _criar_fatura(
        client, empresa, cartao["id"], csrf, competencia="2026-03"
    )
    fatura_b = await _criar_fatura(
        client, empresa, cartao["id"], csrf, competencia="2026-04"
    )
    for fatura in (fatura_a, fatura_b):
        await _add_lancamento(
            client, empresa, cartao["id"], fatura["id"], csrf, 2_500
        )

    payload = {"transacao_id": str(transacao.id)}
    sufixo = f"/{cartao['id']}/faturas/{fatura_a['id']}/associar-transacao"
    assert (
        await client.post(
            _url(empresa.id, sufixo),
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
    ).status_code == 200
    sufixo = f"/{cartao['id']}/faturas/{fatura_b['id']}/associar-transacao"
    assert (
        await client.post(
            _url(empresa.id, sufixo),
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
    ).status_code == 409


# ── lançamentos e total da fatura ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_total_da_fatura_acompanha_os_lancamentos(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 300.50)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 199.50)

    body = (
        await client.get(_url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/lancamentos"))
    ).json()
    assert body["total"] == 2
    assert _money(body["valor_total"]) == Decimal("500.00")

    faturas = (await client.get(_url(empresa.id, f"/{cartao['id']}/faturas"))).json()
    assert _money(faturas["items"][0]["valor_total"]) == Decimal("500.00")


@pytest.mark.asyncio
async def test_remover_lancamento_recalcula_o_total(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    l1 = await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 300)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 200)

    r = await client.delete(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/lancamentos/{l1['id']}"),
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 204

    faturas = (await client.get(_url(empresa.id, f"/{cartao['id']}/faturas"))).json()
    assert _money(faturas["items"][0]["valor_total"]) == Decimal("200.00")


@pytest.mark.asyncio
async def test_fatura_paga_nao_aceita_lancamento_novo(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    """O valor da fatura paga já foi conciliado — mexer nele desalinha a conciliação."""
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 2_500)
    await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/lancamentos"),
        json={"data_compra": "2026-03-05T00:00:00Z", "descricao": "COMPRA TARDIA", "valor": 50},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_valor_de_lancamento_precisa_ser_positivo(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/lancamentos"),
        json={"data_compra": "2026-03-05T00:00:00Z", "descricao": "ESTORNO", "valor": -10},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_lancamento_rejeita_conta_contabil_de_outra_empresa(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    outra = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA CONTA CARTAO LTDA",
        cnpj="44.398.564/0001-35",
        regime_tributario="lucro_real",
    )
    db.add(outra)
    await db.flush()
    conta_outra = PlanoConta(
        empresa_id=outra.id,
        codigo="1.1.01",
        descricao="Conta de outro cliente",
        tipo="ativo",
        tipo_sa="A",
    )
    db.add(conta_outra)
    await db.flush()

    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/lancamentos"),
        json={
            "data_compra": "2026-03-05T00:00:00Z",
            "descricao": "COMPRA",
            "valor": 10,
            "conta_id": str(conta_outra.id),
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_total_da_fatura_e_derivado_dos_lancamentos(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 123.45)
    registro = await db.get(FaturaCartao, uuid.UUID(fatura["id"]))
    assert registro is not None
    registro.valor_total = 999_999
    await db.flush()

    faturas = (await client.get(_url(empresa.id, f"/{cartao['id']}/faturas"))).json()
    assert _money(faturas["items"][0]["valor_total"]) == Decimal("123.45")


# ── importação de CSV ─────────────────────────────────────────────────────────


async def _importar(client, empresa, cartao_id, fatura_id, csrf, csv_texto: str) -> dict:
    r = await client.post(
        _url(empresa.id, f"/{cartao_id}/faturas/{fatura_id}/lancamentos/importar-csv"),
        files={"arquivo": ("fatura.csv", io.BytesIO(csv_texto.encode("utf-8")), "text/csv")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_importar_csv_em_formato_brasileiro(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """dd/mm/yyyy e 1.234,56 — o formato que sai dos bancos brasileiros.

    Os valores vão entre aspas de propósito: a vírgula decimal é também o separador
    do CSV, então sem aspas a linha vira quatro colunas e o valor chega truncado.
    """
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    body = await _importar(
        client, empresa, cartao["id"], fatura["id"], csrf,
        "data_compra,descricao,valor\n"
        '05/03/2026,POSTO IPIRANGA,"1.234,56"\n'
        '06/03/2026,MERCADO LIVRE,"R$ 265,44"\n',
    )
    assert body["importados"] == 2
    assert body["erros"] == []

    faturas = (await client.get(_url(empresa.id, f"/{cartao['id']}/faturas"))).json()
    assert _money(faturas["items"][0]["valor_total"]) == Decimal("1500.00")


@pytest.mark.asyncio
async def test_importar_csv_aceita_cabecalho_em_ingles(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    body = await _importar(
        client, empresa, cartao["id"], fatura["id"], csrf,
        "date,description,amount\n2026-03-07,AWS,150.00\n",
    )
    assert body["importados"] == 1


@pytest.mark.asyncio
async def test_reupload_do_mesmo_csv_nao_duplica_compras(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    csv_texto = (
        "id,data_compra,descricao,valor\n"
        "compra-externa-123,05/03/2026,POSTO IPIRANGA,200.00\n"
    )

    primeira = await _importar(
        client, empresa, cartao["id"], fatura["id"], csrf, csv_texto
    )
    segunda = await _importar(
        client, empresa, cartao["id"], fatura["id"], csrf, csv_texto
    )
    assert primeira["importados"] == 1
    assert segunda["importados"] == 0
    assert segunda["duplicados"] == 1

    lancamentos = (
        await client.get(
            _url(
                empresa.id,
                f"/{cartao['id']}/faturas/{fatura['id']}/lancamentos",
            )
        )
    ).json()
    assert lancamentos["total"] == 1


@pytest.mark.asyncio
async def test_importar_csv_relata_linha_com_erro_sem_perder_as_boas(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Importação parcial: uma linha ruim não pode descartar o arquivo inteiro."""
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    body = await _importar(
        client, empresa, cartao["id"], fatura["id"], csrf,
        "data_compra,descricao,valor\n"
        "05/03/2026,COMPRA BOA,100\n"
        "data_invalida,COMPRA RUIM,200\n"
        "07/03/2026,,300\n"
        "08/03/2026,VALOR NEGATIVO,-50\n",
    )
    assert body["importados"] == 1
    assert len(body["erros"]) == 3
    assert any("Linha 3" in e for e in body["erros"])
    assert any("Linha 4" in e for e in body["erros"])
    assert any("Linha 5" in e for e in body["erros"])


@pytest.mark.asyncio
async def test_importar_csv_em_fatura_paga_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao
):
    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 2_500)
    await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/lancamentos/importar-csv"),
        files={"arquivo": ("f.csv", io.BytesIO(b"data_compra,descricao,valor\n05/03/2026,X,10\n"), "text/csv")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


# ── isolamento entre cartões e empresas ───────────────────────────────────────


@pytest.mark.asyncio
async def test_fatura_de_outro_cartao_nao_e_acessivel(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    cartao_a = await _criar_cartao(client, empresa, csrf, nome="Cartão A")
    cartao_b = await _criar_cartao(client, empresa, csrf, nome="Cartão B", ultimos_digitos="9999")
    fatura_a = await _criar_fatura(client, empresa, cartao_a["id"], csrf)

    r = await client.get(
        _url(empresa.id, f"/{cartao_b['id']}/faturas/{fatura_a['id']}/lancamentos")
    )
    assert r.status_code == 404


# ── importação de PDF ─────────────────────────────────────────────────────────


def _fake_lancamentos_pdf(*specs: tuple[str, str, float]) -> list:
    from src.domain.cartoes.pdf_parser import LancamentoPDF

    resultado = []
    for data_str, descricao, valor in specs:
        dia, mes, ano = (int(p) for p in data_str.split("/"))
        resultado.append(
            LancamentoPDF(
                data_compra=datetime(ano, mes, dia, tzinfo=UTC),
                descricao=descricao,
                valor=Decimal(str(valor)),
            )
        )
    return resultado


async def _importar_pdf(client, empresa, cartao_id, fatura_id, csrf) -> dict:
    r = await client.post(
        _url(empresa.id, f"/{cartao_id}/faturas/{fatura_id}/lancamentos/importar-pdf"),
        files={"arquivo": ("fatura.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        headers={"X-CSRF-Token": csrf},
    )
    return r


@pytest.mark.asyncio
async def test_importar_pdf_persiste_lancamentos_extraidos(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, monkeypatch
):
    from src.domain.cartoes import pdf_parser as pdf_parser_module

    monkeypatch.setattr(
        pdf_parser_module,
        "parse_pdf",
        lambda conteudo: _fake_lancamentos_pdf(
            ("05/03/2026", "POSTO IPIRANGA", 150.00),
            ("06/03/2026", "MERCADO LIVRE", 265.44),
        ),
    )

    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    r = await _importar_pdf(client, empresa, cartao["id"], fatura["id"], csrf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["importados"] == 2
    assert body["erros"] == []

    faturas = (await client.get(_url(empresa.id, f"/{cartao['id']}/faturas"))).json()
    assert _money(faturas["items"][0]["valor_total"]) == Decimal("415.44")


@pytest.mark.asyncio
async def test_reimportar_mesmo_pdf_nao_duplica_lancamentos(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, monkeypatch
):
    from src.domain.cartoes import pdf_parser as pdf_parser_module

    monkeypatch.setattr(
        pdf_parser_module,
        "parse_pdf",
        lambda conteudo: _fake_lancamentos_pdf(("05/03/2026", "POSTO IPIRANGA", 150.00)),
    )

    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    primeira = (await _importar_pdf(client, empresa, cartao["id"], fatura["id"], csrf)).json()
    segunda = (await _importar_pdf(client, empresa, cartao["id"], fatura["id"], csrf)).json()

    assert primeira["importados"] == 1
    assert segunda["importados"] == 0
    assert segunda["duplicados"] == 1


@pytest.mark.asyncio
async def test_importar_pdf_layout_nao_reconhecido_retorna_422(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, monkeypatch
):
    from src.domain.cartoes import pdf_parser as pdf_parser_module

    def _raise(conteudo: bytes):
        raise pdf_parser_module.PDFParseError("Não foi possível extrair lançamentos desta fatura.")

    monkeypatch.setattr(pdf_parser_module, "parse_pdf", _raise)

    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)

    r = await _importar_pdf(client, empresa, cartao["id"], fatura["id"], csrf)
    assert r.status_code == 422
    assert "PDF inválido" in r.json()["message"]


@pytest.mark.asyncio
async def test_importar_pdf_em_fatura_paga_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, transacao: Transacao, monkeypatch
):
    from src.domain.cartoes import pdf_parser as pdf_parser_module

    monkeypatch.setattr(
        pdf_parser_module,
        "parse_pdf",
        lambda conteudo: _fake_lancamentos_pdf(("05/03/2026", "X", 10.00)),
    )

    csrf = await _login(client, tenant, usuario)
    cartao = await _criar_cartao(client, empresa, csrf)
    fatura = await _criar_fatura(client, empresa, cartao["id"], csrf)
    await _add_lancamento(client, empresa, cartao["id"], fatura["id"], csrf, 2_500)
    await client.post(
        _url(empresa.id, f"/{cartao['id']}/faturas/{fatura['id']}/associar-transacao"),
        json={"transacao_id": str(transacao.id)},
        headers={"X-CSRF-Token": csrf},
    )

    r = await _importar_pdf(client, empresa, cartao["id"], fatura["id"], csrf)
    assert r.status_code == 422
