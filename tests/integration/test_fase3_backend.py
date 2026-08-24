"""Regressões dos endpoints operacionais e da falha alta do motor NEO."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.security import hash_password
from src.db.models import (
    AgenciaBancaria,
    AuditLog,
    Empresa,
    NeoDecisao,
    Permissao,
    Transacao,
    Usuario,
)
from src.domain.auditoria import registrar_auditoria
from src.domain.neo.engine import NeoEngine


async def _login(client, tenant, usuario) -> None:
    resposta = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": usuario.email,
            "senha": "senha_segura_123",
        },
    )
    assert resposta.status_code == 200


async def _agencia(db, empresa, sufixo: str) -> AgenciaBancaria:
    agencia = AgenciaBancaria(
        empresa_id=empresa.id,
        banco_sigla="BB",
        agencia=f"1{sufixo}",
        numero=f"9{sufixo}",
    )
    db.add(agencia)
    await db.flush()
    return agencia


def _transacao(empresa, agencia, *, status: str, valor: str, hash_dedup: str) -> Transacao:
    return Transacao(
        empresa_id=empresa.id,
        agencia_id=agencia.id,
        data=date(2026, 8, 10),
        valor=Decimal(valor),
        historico=f"MOVIMENTO {hash_dedup}",
        dc="D",
        status=status,
        hash_dedup=hash_dedup,
    )


@pytest.mark.asyncio
async def test_neo_propaga_attribute_error_sem_transformar_extrato_em_erros(
    db, empresa, monkeypatch
):
    """Um atributo removido é defeito do motor, não dado ruim da transação.

    Este teste trava o incidente em que o `AttributeError` era engolido e todas
    as linhas do extrato recebiam decisões `erro`, ocultando a causa real.
    """
    agencia = await _agencia(db, empresa, "01")
    db.add_all(
        [
            _transacao(
                empresa,
                agencia,
                status="pendente",
                valor="10.00",
                hash_dedup=f"bug-programacao-{indice}",
            )
            for indice in range(3)
        ]
    )
    await db.flush()

    engine = NeoEngine(db=db, empresa_id=empresa.id)

    def atributo_removido(_transacao, _regras):
        raise AttributeError("atributo removido na refatoração")

    monkeypatch.setattr(engine, "_encontrar_regra", atributo_removido)

    with pytest.raises(AttributeError, match="atributo removido"):
        await engine.processar()

    decisoes = (
        await db.execute(
            select(func.count()).select_from(NeoDecisao).where(
                NeoDecisao.empresa_id == empresa.id,
                NeoDecisao.resultado == "erro",
            )
        )
    ).scalar_one()
    assert decisoes == 0, "o bug não pode virar uma decisão de dado por transação"


@pytest.mark.asyncio
async def test_auditoria_lista_ator_json_filtros_e_paginacao(
    client, db, tenant, usuario, empresa
):
    """A trilha só é útil se identifica a pessoa e devolve o diff legível.

    Trava também os filtros combináveis e a paginação que impedem a tela de
    carregar todo o histórico de uma empresa de uma vez.
    """
    primeiro = await registrar_auditoria(
        db,
        tenant_id=tenant.id,
        empresa_id=empresa.id,
        usuario_id=usuario.id,
        acao="regra.atualizada",
        entidade="regra",
        entidade_id="regra-1",
        dados_antes={"descricao": "Antes"},
        dados_depois={"descricao": "Depois"},
    )
    primeiro.created_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
    segundo = await registrar_auditoria(
        db,
        tenant_id=tenant.id,
        empresa_id=empresa.id,
        usuario_id=usuario.id,
        acao="regra.atualizada",
        entidade="regra",
        entidade_id="regra-2",
        dados_depois={"ativa": False},
    )
    segundo.created_at = datetime(2026, 8, 6, 12, tzinfo=UTC)
    fora_do_mes = await registrar_auditoria(
        db,
        tenant_id=tenant.id,
        empresa_id=empresa.id,
        usuario_id=usuario.id,
        acao="regra.atualizada",
        entidade="regra",
        entidade_id="regra-3",
    )
    fora_do_mes.created_at = datetime(2026, 7, 31, 12, tzinfo=UTC)
    await db.flush()
    await _login(client, tenant, usuario)

    resposta = await client.get(
        f"/api/v1/empresas/{empresa.id}/auditoria",
        params={
            "usuario_id": str(usuario.id),
            "acao": "regra.atualizada",
            "entidade": "regra",
            "mes": "2026-08",
            "data_de": "2026-08-05",
            "data_ate": "2026-08-06",
            "page": 2,
            "page_size": 1,
        },
    )

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["total"] == 2
    assert corpo["page"] == 2
    assert corpo["page_size"] == 1
    assert corpo["items"][0]["entidade_id"] == "regra-1"
    assert corpo["items"][0]["usuario_nome"] == usuario.nome
    assert corpo["items"][0]["usuario_email"] == usuario.email
    assert corpo["items"][0]["dados_antes"] == {"descricao": "Antes"}
    assert corpo["items"][0]["dados_depois"] == {"descricao": "Depois"}


@pytest.mark.asyncio
async def test_auditoria_rejeita_contador_mesmo_com_acesso_a_empresa(
    client, db, tenant, empresa
):
    """A auditoria revela a atividade de colegas e deve ficar restrita ao admin.

    Conceder acesso total à empresa não pode ampliar silenciosamente esse dado
    sensível para um contador comum.
    """
    contador = Usuario(
        tenant_id=tenant.id,
        email="contador-auditoria@example.com",
        nome="Contador sem auditoria",
        senha_hash=hash_password("senha_segura_123"),
        role="contador",
    )
    db.add(contador)
    await db.flush()
    db.add(Permissao(usuario_id=contador.id, empresa_id=empresa.id, modulos="*"))
    await db.flush()
    await _login(client, tenant, contador)

    resposta = await client.get(f"/api/v1/empresas/{empresa.id}/auditoria")

    assert resposta.status_code == 403


@pytest.mark.asyncio
async def test_carteira_agrega_estados_inclui_empresa_sem_extrato_e_ordena_pendencias(
    client, db, tenant, usuario, empresa
):
    """Sem extrato e tudo classificado são estados opostos para a operação.

    A resposta deve preservar empresas sem movimento, somar cada status e trazer
    primeiro onde há mais pendências, sem confundir zero pendente com zero importado.
    """
    sem_extrato = Empresa(
        tenant_id=tenant.id,
        razao_social="EMPRESA SEM EXTRATO LTDA",
        cnpj="10.000.000/0001-00",
        regime_tributario="simples_nacional",
    )
    classificada = Empresa(
        tenant_id=tenant.id,
        razao_social="EMPRESA CLASSIFICADA LTDA",
        cnpj="20.000.000/0001-00",
        regime_tributario="simples_nacional",
    )
    db.add_all([sem_extrato, classificada])
    await db.flush()
    agencia_pendente = await _agencia(db, empresa, "11")
    agencia_classificada = await _agencia(db, classificada, "12")
    db.add_all(
        [
            _transacao(
                empresa,
                agencia_pendente,
                status="pendente",
                valor="100.50",
                hash_dedup="carteira-pendente-1",
            ),
            _transacao(
                empresa,
                agencia_pendente,
                status="pendente",
                valor="20.00",
                hash_dedup="carteira-pendente-2",
            ),
            _transacao(
                empresa,
                agencia_pendente,
                status="erro",
                valor="3.00",
                hash_dedup="carteira-erro",
            ),
            _transacao(
                classificada,
                agencia_classificada,
                status="processada",
                valor="70.00",
                hash_dedup="carteira-processada",
            ),
        ]
    )
    await db.flush()
    await _login(client, tenant, usuario)

    resposta = await client.get("/api/v1/carteira", params={"mes": "2026-08"})

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["mes"] == "2026-08"
    itens = corpo["items"]
    assert itens[0]["empresa_id"] == str(empresa.id)
    por_id = {item["empresa_id"]: item for item in itens}
    pendente = por_id[str(empresa.id)]
    assert pendente == {
        "empresa_id": str(empresa.id),
        "razao_social": empresa.razao_social,
        "transacoes_importadas": 3,
        "pendentes": 2,
        "classificadas": 0,
        "erros": 1,
        "ha_extrato_importado": True,
        "valor_total_pendente": "120.50",
    }
    assert por_id[str(sem_extrato.id)]["ha_extrato_importado"] is False
    assert por_id[str(sem_extrato.id)]["transacoes_importadas"] == 0
    assert por_id[str(classificada.id)]["ha_extrato_importado"] is True
    assert por_id[str(classificada.id)]["pendentes"] == 0
    assert por_id[str(classificada.id)]["classificadas"] == 1


@pytest.mark.asyncio
async def test_carteira_do_contador_respeita_permissoes_por_empresa(
    client, db, tenant, empresa
):
    """A visão do escritório não pode furar o escopo aplicado nas empresas.

    O teste trava a regressão mais perigosa de uma consulta global: um contador
    receber dados operacionais de clientes para os quais não tem permissão.
    """
    invisivel = Empresa(
        tenant_id=tenant.id,
        razao_social="EMPRESA FORA DO ESCOPO LTDA",
        cnpj="30.000.000/0001-00",
        regime_tributario="simples_nacional",
    )
    contador = Usuario(
        tenant_id=tenant.id,
        email="contador-carteira@example.com",
        nome="Contador Carteira",
        senha_hash=hash_password("senha_segura_123"),
        role="contador",
    )
    db.add_all([invisivel, contador])
    await db.flush()
    db.add(Permissao(usuario_id=contador.id, empresa_id=empresa.id, modulos="*"))
    await db.flush()
    await _login(client, tenant, contador)

    resposta = await client.get("/api/v1/carteira", params={"mes": "2026-08"})

    assert resposta.status_code == 200, resposta.text
    ids = {item["empresa_id"] for item in resposta.json()["items"]}
    assert ids == {str(empresa.id)}
    assert str(invisivel.id) not in ids
