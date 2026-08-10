"""Cobertura do trilho central de auditoria nas mutações priorizadas."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from src.core.context import set_request_context
from src.core.security import hash_password
from src.db.models import AuditLog, Usuario
from src.domain.cartoes.service import CartaoService
from src.domain.empresas.service import EmpresaService
from src.domain.openbanking.service import OpenBankingService
from src.domain.permissoes.service import PermissaoService
from src.domain.plano_contas.service import PlanoContaService
from src.schemas.cartoes import CartaoCreate, FaturaCreate, FaturaUpdate
from src.schemas.empresas import EmpresaCreate, EmpresaUpdate
from src.schemas.openbanking import SalvarConexaoRequest
from src.schemas.permissoes import PermissaoCreate, PermissaoUpdate
from src.schemas.plano_contas import PlanoContaCreate, PlanoContaUpdate


@pytest.mark.asyncio
async def test_mutacoes_prioritarias_gravam_auditoria_com_ator_e_antes_depois(
    db, tenant, usuario, empresa
):
    set_request_context("trace-auditoria", user_id=usuario.id, company_id=empresa.id)

    empresas = EmpresaService(db, tenant.id, usuario.id, "admin")
    criada = await empresas.criar(
        EmpresaCreate(
            razao_social="EMPRESA AUDITADA LTDA",
            cnpj="52.540.787/0001-88",
            regime_tributario="lucro_real",
        )
    )
    await empresas.atualizar(
        criada.id, EmpresaUpdate(razao_social="EMPRESA AUDITADA NOVO NOME LTDA")
    )
    await empresas.desativar(criada.id)

    alvo = Usuario(
        tenant_id=tenant.id,
        email="auditado@example.com",
        nome="Usuário Auditado",
        senha_hash=hash_password("senha_segura_123"),
        role="contador",
    )
    db.add(alvo)
    await db.flush()
    permissoes = PermissaoService(db, empresa.id, tenant.id)
    await permissoes.conceder(PermissaoCreate(usuario_id=alvo.id, modulos="extrato"))
    await permissoes.atualizar(alvo.id, PermissaoUpdate(modulos="extrato,notas"))
    await permissoes.revogar(alvo.id)

    plano = PlanoContaService(db, empresa.id)
    conta = await plano.criar(
        PlanoContaCreate(codigo="9.9.1", descricao="Conta Auditada", tipo="despesa")
    )
    await plano.atualizar(conta.id, PlanoContaUpdate(descricao="Conta Auditada Atualizada"))
    await plano.remover(conta.id)

    cartoes = CartaoService(db, empresa.id)
    cartao = await cartoes.criar_cartao(
        CartaoCreate(
            nome="Cartão Auditado",
            bandeira="visa",
            dia_fechamento=10,
            dia_vencimento=20,
        )
    )
    fatura = await cartoes.criar_fatura(cartao.id, FaturaCreate(competencia="2026-08"))
    await cartoes.atualizar_fatura(cartao.id, fatura.id, FaturaUpdate(status="fechada"))

    openbanking = OpenBankingService(db, empresa.id)
    token = await openbanking.criar_connect_token()
    assert token.connection_session is not None
    await openbanking.salvar_conexao(
        SalvarConexaoRequest(
            item_id="item_auditoria",
            connection_session=token.connection_session,
        )
    )

    logs = (await db.execute(select(AuditLog))).scalars().all()
    acoes = {log.acao for log in logs}
    assert {
        "empresa.criada",
        "empresa.atualizada",
        "empresa.desativada",
        "permissao.concedida",
        "permissao.atualizada",
        "permissao.revogada",
        "plano_conta.criada",
        "plano_conta.atualizada",
        "plano_conta.removida",
        "fatura.status_atualizado",
        "openbanking.conexao_criada",
    } <= acoes

    atualizacao = next(log for log in logs if log.acao == "plano_conta.atualizada")
    assert atualizacao.usuario_id == usuario.id
    assert atualizacao.tenant_id == tenant.id
    assert atualizacao.trace_id == "trace-auditoria"
    assert json.loads(atualizacao.dados_antes)["descricao"] == "Conta Auditada"
    assert json.loads(atualizacao.dados_depois)["descricao"] == "Conta Auditada Atualizada"
