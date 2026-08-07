"""Testes de integração — Relatórios financeiros (DRE, Balancete, Livro Caixa).

São relatórios contábeis: o valor deles é a aritmética estar certa, não o endpoint
responder 200. Os testes montam movimento conhecido e conferem o número esperado —
sinal de saldo por natureza da conta, identidade do balancete, saldo acumulado.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgenciaBancaria, Empresa, PlanoConta, RegistroContabil, Tenant, Transacao, Usuario


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, recurso: str) -> str:
    return f"/api/v1/empresas/{empresa_id}/relatorios/{recurso}"


@pytest_asyncio.fixture
async def contas(db: AsyncSession, empresa: Empresa) -> dict[str, PlanoConta]:
    criadas = {
        "receita": PlanoConta(
            empresa_id=empresa.id, codigo="3.1.1", descricao="Receita de Serviços", tipo="receita"
        ),
        "despesa": PlanoConta(
            empresa_id=empresa.id, codigo="4.1.1", descricao="Despesas Administrativas", tipo="despesa"
        ),
        "custo": PlanoConta(
            empresa_id=empresa.id, codigo="3.2.1", descricao="Custo dos Serviços", tipo="custo"
        ),
        "sem_movimento": PlanoConta(
            empresa_id=empresa.id, codigo="1.1.1", descricao="Caixa", tipo="ativo"
        ),
    }
    for c in criadas.values():
        db.add(c)
    await db.flush()
    return criadas


@pytest_asyncio.fixture
async def movimento(db: AsyncSession, empresa: Empresa, contas: dict, agencia: AgenciaBancaria):
    """Receita 10.000 C · Despesa 3.000 D · Custo 2.000 D → resultado 5.000."""
    lancamentos = [
        (contas["receita"], "C", 10_000),
        (contas["despesa"], "D", 3_000),
        (contas["custo"], "D", 2_000),
    ]
    for conta, dc, valor in lancamentos:
        db.add(
            RegistroContabil(
                empresa_id=empresa.id,
                conta_id=conta.id,
                agencia_id=agencia.id,
                descricao=conta.descricao,
                historico=conta.descricao,
                historico_extrato=conta.descricao,
                dc=dc,
                tipo_regra="automatica",
                valor=valor,
                data_lancamento=datetime(2026, 3, 15, tzinfo=UTC),
            )
        )
    await db.flush()


@pytest_asyncio.fixture
async def agencia(db: AsyncSession, empresa: Empresa) -> AgenciaBancaria:
    a = AgenciaBancaria(
        empresa_id=empresa.id, banco_sigla="ITAU", agencia="1234", numero="56789", digito="0"
    )
    db.add(a)
    await db.flush()
    return a


# ── DRE ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dre_sem_autenticacao_rejeita(client: AsyncClient, empresa: Empresa):
    r = await client.get(_url(empresa.id, "dre"))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_dre_vazio_zera_totais(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id, "dre"))
    assert r.status_code == 200
    body = r.json()
    assert body["grupos"] == []
    assert body["resultado_liquido"] == 0


@pytest.mark.asyncio
async def test_dre_calcula_resultado_liquido(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, movimento
):
    """Resultado = Receitas − Custos − Despesas = 10.000 − 2.000 − 3.000."""
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id, "dre"))
    assert r.status_code == 200

    body = r.json()
    assert body["total_receitas"] == 10_000
    assert body["total_custos"] == 2_000
    assert body["total_despesas"] == 3_000
    assert body["resultado_liquido"] == 5_000


@pytest.mark.asyncio
async def test_dre_preserva_movimento_de_conta_legada_removida(
    client, db, tenant, usuario, empresa, contas, movimento
):
    contas["receita"].deleted_at = datetime.now(UTC)
    await db.flush()
    await _login(client, tenant, usuario)

    r = await client.get(_url(empresa.id, "dre"))

    receitas = next(grupo for grupo in r.json()["grupos"] if grupo["tipo"] == "receita")
    assert receitas["total"] == 10_000


@pytest.mark.asyncio
async def test_dre_respeita_natureza_da_conta(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, movimento
):
    """Receita é credora (C − D); despesa é devedora (D − C). Trocar o sinal inverteria o DRE."""
    await _login(client, tenant, usuario)
    grupos = {g["tipo"]: g for g in (await client.get(_url(empresa.id, "dre"))).json()["grupos"]}

    receita = grupos["receita"]["linhas"][0]
    assert receita["creditos"] == 10_000
    assert receita["debitos"] == 0
    assert receita["saldo"] == 10_000  # C − D

    despesa = grupos["despesa"]["linhas"][0]
    assert despesa["debitos"] == 3_000
    assert despesa["saldo"] == 3_000  # D − C


@pytest.mark.asyncio
async def test_dre_filtra_por_periodo(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, movimento
):
    """Período fora do movimento zera o relatório — o filtro de data precisa morder."""
    await _login(client, tenant, usuario)
    r = await client.get(
        _url(empresa.id, "dre"),
        params={"data_de": "2026-01-01T00:00:00Z", "data_ate": "2026-01-31T23:59:59Z"},
    )
    assert r.status_code == 200
    assert r.json()["resultado_liquido"] == 0


@pytest.mark.asyncio
async def test_dre_ignora_contas_patrimoniais(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    agencia: AgenciaBancaria,
    movimento,
):
    passivo = PlanoConta(
        empresa_id=empresa.id,
        codigo="2.1.1",
        descricao="Fornecedores",
        tipo="passivo",
    )
    db.add(passivo)
    await db.flush()
    db.add(
        RegistroContabil(
            empresa_id=empresa.id,
            conta_id=passivo.id,
            agencia_id=agencia.id,
            descricao="Compra a prazo",
            historico="Compra a prazo",
            historico_extrato="Compra a prazo",
            dc="C",
            tipo_regra="manual",
            valor=9_999,
            data_lancamento=datetime(2026, 3, 15, tzinfo=UTC),
        )
    )
    await db.flush()

    await _login(client, tenant, usuario)
    body = (await client.get(_url(empresa.id, "dre"))).json()

    assert "passivo" not in {grupo["tipo"] for grupo in body["grupos"]}
    assert body["resultado_liquido"] == 5_000


# ── Balancete ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_balancete_fecha_debitos_e_creditos(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, movimento
):
    """Partidas dobradas: com o movimento equilibrado, total D = total C."""
    await _login(client, tenant, usuario)
    body = (await client.get(_url(empresa.id, "balancete"))).json()

    assert body["total_debitos"] == 5_000     # 3.000 despesa + 2.000 custo
    assert body["total_creditos"] == 10_000   # receita
    assert body["total_saldo_devedor"] == 5_000
    assert body["total_saldo_credor"] == 10_000


@pytest.mark.asyncio
async def test_balancete_saldos_sao_mutuamente_exclusivos(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, movimento
):
    """Uma conta é devedora OU credora, nunca as duas."""
    await _login(client, tenant, usuario)
    linhas = (await client.get(_url(empresa.id, "balancete"))).json()["linhas"]

    for linha in linhas:
        assert linha["saldo_devedor"] == 0 or linha["saldo_credor"] == 0, linha["codigo"]


@pytest.mark.asyncio
async def test_balancete_inclui_conta_sem_movimento(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, contas, movimento
):
    """Conta zerada tem que aparecer — sumir dela esconde conta que deveria ter movimento."""
    await _login(client, tenant, usuario)
    linhas = (await client.get(_url(empresa.id, "balancete"))).json()["linhas"]

    caixa = [l for l in linhas if l["codigo"] == "1.1.1"]
    assert len(caixa) == 1
    assert caixa[0]["debitos"] == 0
    assert caixa[0]["creditos"] == 0


@pytest.mark.asyncio
async def test_balancete_ordena_por_codigo(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa, contas, movimento
):
    await _login(client, tenant, usuario)
    codigos = [l["codigo"] for l in (await client.get(_url(empresa.id, "balancete"))).json()["linhas"]]
    assert codigos == sorted(codigos)


# ── Livro Caixa ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_livro_caixa_sem_agencia_volta_vazio(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id, "livro-caixa"))
    assert r.status_code == 200
    assert r.json()["agencias"] == []


@pytest.mark.asyncio
async def test_livro_caixa_acumula_saldo_na_ordem_das_datas(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    agencia: AgenciaBancaria,
):
    """C soma, D subtrai, e o acumulado tem que seguir a cronologia."""
    entradas = [
        (datetime(2026, 3, 1, tzinfo=UTC), "C", 1_000, "DEPOSITO"),
        (datetime(2026, 3, 2, tzinfo=UTC), "D", 400, "PAGAMENTO FORNECEDOR"),
        (datetime(2026, 3, 3, tzinfo=UTC), "C", 250, "TED RECEBIDA"),
    ]
    for i, (data, dc, valor, historico) in enumerate(entradas):
        db.add(
            Transacao(
                empresa_id=empresa.id,
                agencia_id=agencia.id,
                data=data,
                valor=valor,
                historico=historico,
                dc=dc,
                hash_dedup=f"hash_livro_caixa_{i}",
            )
        )
    await db.flush()

    await _login(client, tenant, usuario)
    body = (await client.get(_url(empresa.id, "livro-caixa"))).json()

    assert len(body["agencias"]) == 1
    ag = body["agencias"][0]
    assert ag["descricao"] == "ITAU 1234 56789 0"
    assert [l["saldo_acumulado"] for l in ag["lancamentos"]] == [1_000, 600, 850]
    assert ag["saldo_final"] == 850
    assert ag["total_creditos"] == 1_250
    assert ag["total_debitos"] == 400


@pytest.mark.asyncio
async def test_livro_caixa_saldo_inicial_vem_do_que_antecede_o_periodo(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    agencia: AgenciaBancaria,
):
    """O que ficou para trás vira saldo inicial, não some do relatório."""
    db.add(
        Transacao(
            empresa_id=empresa.id, agencia_id=agencia.id,
            data=datetime(2026, 2, 10, tzinfo=UTC), valor=5_000,
            historico="SALDO ANTERIOR", dc="C", hash_dedup="hash_anterior",
        )
    )
    db.add(
        Transacao(
            empresa_id=empresa.id, agencia_id=agencia.id,
            data=datetime(2026, 3, 5, tzinfo=UTC), valor=1_000,
            historico="PAGAMENTO", dc="D", hash_dedup="hash_no_periodo",
        )
    )
    await db.flush()

    await _login(client, tenant, usuario)
    body = (
        await client.get(
            _url(empresa.id, "livro-caixa"),
            params={"data_de": "2026-03-01T00:00:00Z", "data_ate": "2026-03-31T23:59:59Z"},
        )
    ).json()

    ag = body["agencias"][0]
    assert ag["saldo_inicial"] == 5_000
    assert len(ag["lancamentos"]) == 1, "a transação de fevereiro não pode entrar no período"
    assert ag["saldo_final"] == 4_000
