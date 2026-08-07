"""Testes de integração — Stats do Dashboard.

O dashboard é o primeiro lugar onde alguém olha para saber se a conciliação está
andando. Um percentual errado aqui não parece um bug — parece trabalho pendente.
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


def _url(empresa_id) -> str:
    return f"/api/v1/empresas/{empresa_id}/stats"


@pytest_asyncio.fixture
async def agencia(db: AsyncSession, empresa: Empresa) -> AgenciaBancaria:
    a = AgenciaBancaria(
        empresa_id=empresa.id, banco_sigla="BB", agencia="2456", numero="72426", digito="2"
    )
    db.add(a)
    await db.flush()
    return a


@pytest_asyncio.fixture
async def conta(db: AsyncSession, empresa: Empresa) -> PlanoConta:
    c = PlanoConta(
        empresa_id=empresa.id, codigo="3.1.1", descricao="Receitas", tipo="receita"
    )
    db.add(c)
    await db.flush()
    return c


@pytest_asyncio.fixture
async def tres_transacoes_uma_conciliada(
    db: AsyncSession, empresa: Empresa, agencia: AgenciaBancaria, conta: PlanoConta
) -> list[Transacao]:
    """3 transações, 1 delas com registro contábil → 33,3% de conciliação."""
    transacoes = []
    for i in range(3):
        t = Transacao(
            empresa_id=empresa.id,
            agencia_id=agencia.id,
            data=datetime(2026, 3, 10 + i, tzinfo=UTC),
            valor=100 * (i + 1),
            historico=f"MOVIMENTO {i}",
            dc="C",
            hash_dedup=f"hash_stats_{i}",
        )
        db.add(t)
        transacoes.append(t)
    await db.flush()

    db.add(
        RegistroContabil(
            empresa_id=empresa.id,
            transacao_id=transacoes[0].id,
            conta_id=conta.id,
            agencia_id=agencia.id,
            descricao="Receita",
            historico="MOVIMENTO 0",
            historico_extrato="MOVIMENTO 0",
            dc="C",
            tipo_regra="automatica",
            valor=100,
            data_lancamento=datetime(2026, 3, 10, tzinfo=UTC),
        )
    )
    await db.flush()
    return transacoes


# ── acesso ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_sem_autenticacao_rejeita(client: AsyncClient, empresa: Empresa):
    r = await client.get(_url(empresa.id))
    assert r.status_code == 401


# ── resumo ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_empresa_sem_movimento(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Sem transação nenhuma, o percentual precisa ser 0 — e não estourar por divisão por zero."""
    await _login(client, tenant, usuario)
    r = await client.get(_url(empresa.id))
    assert r.status_code == 200

    resumo = r.json()["resumo"]
    assert resumo["total_transacoes"] == 0
    assert resumo["percentual_conciliacao"] == 0


@pytest.mark.asyncio
async def test_stats_conta_conciliadas_e_nao_conciliadas(
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    tres_transacoes_uma_conciliada,
):
    await _login(client, tenant, usuario)
    resumo = (await client.get(_url(empresa.id))).json()["resumo"]

    assert resumo["total_transacoes"] == 3
    assert resumo["total_conciliados"] == 1
    assert resumo["total_nao_conciliados"] == 2
    assert resumo["total_registros"] == 1
    assert resumo["percentual_conciliacao"] == 33.3


@pytest.mark.asyncio
async def test_stats_conciliados_mais_nao_conciliados_fecham_o_total(
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    tres_transacoes_uma_conciliada,
):
    """Invariante: nenhuma transação pode ficar fora das duas categorias."""
    await _login(client, tenant, usuario)
    resumo = (await client.get(_url(empresa.id))).json()["resumo"]

    assert resumo["total_conciliados"] + resumo["total_nao_conciliados"] == resumo["total_transacoes"]


@pytest.mark.asyncio
async def test_stats_ignora_transacao_soft_deleted_em_todas_as_agregacoes(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    tres_transacoes_uma_conciliada,
):
    # A removida é justamente a única conciliada; nem o registro contábil ainda
    # ligado a ela pode fazê-la reaparecer no resumo ou na agência.
    tres_transacoes_uma_conciliada[0].deleted_at = datetime.now(UTC)
    await db.flush()

    await _login(client, tenant, usuario)
    body = (await client.get(_url(empresa.id))).json()

    assert body["resumo"]["total_transacoes"] == 2
    assert body["resumo"]["total_conciliados"] == 0
    assert body["resumo"]["total_nao_conciliados"] == 2
    assert body["por_agencia"][0]["conciliados"] == 0
    assert body["por_agencia"][0]["nao_conciliados"] == 2
    assert sum(mes["transacoes"] for mes in body["mensal"]) == 2


@pytest.mark.asyncio
async def test_stats_nao_conta_transacao_de_outra_empresa(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    tres_transacoes_uma_conciliada,
):
    """Isolamento: movimento de outra empresa não pode inflar o dashboard desta."""
    outra = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA EMPRESA LTDA",
        cnpj="52.540.787/0001-88",
        regime_tributario="lucro_real",
    )
    db.add(outra)
    await db.flush()

    ag_outra = AgenciaBancaria(
        empresa_id=outra.id, banco_sigla="ITAU", agencia="0001", numero="99999"
    )
    db.add(ag_outra)
    await db.flush()

    db.add(
        Transacao(
            empresa_id=outra.id,
            agencia_id=ag_outra.id,
            data=datetime(2026, 3, 20, tzinfo=UTC),
            valor=9_999,
            historico="MOVIMENTO DA OUTRA EMPRESA",
            dc="C",
            hash_dedup="hash_outra_empresa",
        )
    )
    await db.flush()

    await _login(client, tenant, usuario)
    resumo = (await client.get(_url(empresa.id))).json()["resumo"]
    assert resumo["total_transacoes"] == 3


# ── por agência ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_por_agencia(
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    agencia: AgenciaBancaria,
    tres_transacoes_uma_conciliada,
):
    await _login(client, tenant, usuario)
    por_agencia = (await client.get(_url(empresa.id))).json()["por_agencia"]

    assert len(por_agencia) == 1
    ag = por_agencia[0]
    assert ag["agencia_id"] == str(agencia.id)
    assert ag["conciliados"] == 1
    assert ag["nao_conciliados"] == 2


@pytest.mark.asyncio
async def test_stats_ignora_agencia_inativa(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    agencia: AgenciaBancaria,
):
    agencia.ativa = False
    await db.flush()

    await _login(client, tenant, usuario)
    assert (await client.get(_url(empresa.id))).json()["por_agencia"] == []


# ── série mensal ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_serie_mensal_respeita_o_parametro_meses(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    body = (await client.get(_url(empresa.id), params={"meses": 3})).json()

    assert len(body["mensal"]) == 3
    # Chaves no formato YYYY-MM e em ordem cronológica
    chaves = [m["mes"] for m in body["mensal"]]
    assert chaves == sorted(chaves)
    assert all(len(k) == 7 and k[4] == "-" for k in chaves)


@pytest.mark.asyncio
async def test_stats_meses_fora_do_intervalo_permitido_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    assert (await client.get(_url(empresa.id), params={"meses": 0})).status_code == 422
    assert (await client.get(_url(empresa.id), params={"meses": 25})).status_code == 422
