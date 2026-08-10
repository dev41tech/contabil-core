"""Serviço de Aplicações Financeiras.

Regras de negócio:
- Uma empresa pode ter múltiplas aplicações financeiras (CDB, poupança, fundo etc.).
- `agencia_id`, quando informado, precisa pertencer à mesma empresa.
- Atualizar `valor_atual` também atualiza `data_atualizacao_valor` (histórico
  de quando o rendimento foi registrado pela última vez).
- Encerrar (ativa=False) não deleta — preserva o registro para histórico.
- Soft delete via deleted_at, para remover um cadastro feito por engano.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError, ValidationError
from src.db.models import AgenciaBancaria, AplicacaoFinanceira
from src.domain.auditoria import registrar_auditoria
from src.schemas.aplicacoes import (
    AplicacaoFinanceiraCreate,
    AplicacaoFinanceiraListResponse,
    AplicacaoFinanceiraResponse,
    AplicacaoFinanceiraUpdate,
)

logger = structlog.get_logger(__name__)

_NAO_ENCONTRADA = "Aplicação financeira não encontrada."


class AplicacaoFinanceiraService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def listar(self, apenas_ativas: bool = False) -> AplicacaoFinanceiraListResponse:
        q = select(AplicacaoFinanceira).where(
            AplicacaoFinanceira.empresa_id == self._empresa_id,
            AplicacaoFinanceira.deleted_at == None,
        )
        if apenas_ativas:
            q = q.where(AplicacaoFinanceira.ativa == True)
        q = q.order_by(AplicacaoFinanceira.data_aplicacao.desc())

        rows = (await self._db.execute(q)).scalars().all()

        total_q = select(func.count()).where(
            AplicacaoFinanceira.empresa_id == self._empresa_id,
            AplicacaoFinanceira.deleted_at == None,
        )
        total = (await self._db.execute(total_q)).scalar_one()

        valor_total_aplicado = sum((r.valor_aplicado for r in rows), Decimal("0.00"))
        valor_total_atual = sum(
            (r.valor_atual if r.valor_atual is not None else r.valor_aplicado for r in rows),
            Decimal("0.00"),
        )

        return AplicacaoFinanceiraListResponse(
            items=[_to_response(r) for r in rows],
            total=total,
            valor_total_aplicado=valor_total_aplicado,
            valor_total_atual=valor_total_atual,
        )

    async def obter(self, aplicacao_id: UUID) -> AplicacaoFinanceiraResponse:
        aplicacao = await self._get_or_404(aplicacao_id)
        return _to_response(aplicacao)

    async def criar(self, data: AplicacaoFinanceiraCreate) -> AplicacaoFinanceiraResponse:
        if data.agencia_id:
            await self._validar_agencia(data.agencia_id)

        aplicacao = AplicacaoFinanceira(
            empresa_id=self._empresa_id,
            agencia_id=data.agencia_id,
            instituicao=data.instituicao,
            tipo=data.tipo,
            descricao=data.descricao,
            valor_aplicado=data.valor_aplicado,
            data_aplicacao=data.data_aplicacao,
            valor_atual=data.valor_atual,
            data_atualizacao_valor=datetime.now(UTC) if data.valor_atual is not None else None,
            data_vencimento=data.data_vencimento,
            observacao=data.observacao,
        )
        self._db.add(aplicacao)
        await self._db.flush()

        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="aplicacao_financeira.criada",
            entidade="aplicacao_financeira",
            entidade_id=aplicacao.id,
            dados_depois=_snapshot(aplicacao),
        )
        logger.info(
            "aplicacao_financeira.criada",
            aplicacao_id=str(aplicacao.id),
            empresa_id=str(self._empresa_id),
            tipo=aplicacao.tipo,
        )
        return _to_response(aplicacao)

    async def atualizar(
        self, aplicacao_id: UUID, data: AplicacaoFinanceiraUpdate
    ) -> AplicacaoFinanceiraResponse:
        aplicacao = await self._get_or_404(aplicacao_id)
        antes = _snapshot(aplicacao)

        if data.agencia_id is not None:
            await self._validar_agencia(data.agencia_id)
            aplicacao.agencia_id = data.agencia_id
        if data.instituicao is not None:
            aplicacao.instituicao = data.instituicao
        if data.tipo is not None:
            aplicacao.tipo = data.tipo
        if data.descricao is not None:
            aplicacao.descricao = data.descricao
        if data.valor_atual is not None:
            aplicacao.valor_atual = data.valor_atual
            aplicacao.data_atualizacao_valor = datetime.now(UTC)
        if data.data_vencimento is not None:
            aplicacao.data_vencimento = data.data_vencimento
        if data.observacao is not None:
            aplicacao.observacao = data.observacao
        if data.ativa is not None:
            aplicacao.ativa = data.ativa

        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="aplicacao_financeira.atualizada",
            entidade="aplicacao_financeira",
            entidade_id=aplicacao.id,
            dados_antes=antes,
            dados_depois=_snapshot(aplicacao),
        )
        logger.info("aplicacao_financeira.atualizada", aplicacao_id=str(aplicacao_id))
        return _to_response(aplicacao)

    async def remover(self, aplicacao_id: UUID) -> None:
        """Remove (soft delete) um cadastro — para engano de cadastro, não para resgate."""
        aplicacao = await self._get_or_404(aplicacao_id)
        antes = _snapshot(aplicacao)

        aplicacao.deleted_at = datetime.now(UTC)
        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="aplicacao_financeira.removida",
            entidade="aplicacao_financeira",
            entidade_id=aplicacao.id,
            dados_antes=antes,
            dados_depois=_snapshot(aplicacao),
        )
        logger.info("aplicacao_financeira.removida", aplicacao_id=str(aplicacao_id))

    # ── helpers privados ─────────────────────────────────────────────────────

    async def _get_or_404(self, aplicacao_id: UUID) -> AplicacaoFinanceira:
        result = await self._db.execute(
            select(AplicacaoFinanceira).where(
                AplicacaoFinanceira.id == aplicacao_id,
                AplicacaoFinanceira.empresa_id == self._empresa_id,
                AplicacaoFinanceira.deleted_at == None,
            )
        )
        aplicacao = result.scalar_one_or_none()
        if not aplicacao:
            raise NotFoundError(message=_NAO_ENCONTRADA)
        return aplicacao

    async def _validar_agencia(self, agencia_id: UUID) -> None:
        result = await self._db.execute(
            select(AgenciaBancaria).where(
                AgenciaBancaria.id == agencia_id,
                AgenciaBancaria.empresa_id == self._empresa_id,
                AgenciaBancaria.deleted_at == None,
            )
        )
        if result.scalar_one_or_none() is None:
            raise ValidationError(
                message="Conta bancária informada não pertence a esta empresa."
            )


def _to_response(a: AplicacaoFinanceira) -> AplicacaoFinanceiraResponse:
    return AplicacaoFinanceiraResponse(
        id=a.id,
        empresa_id=a.empresa_id,
        agencia_id=a.agencia_id,
        instituicao=a.instituicao,
        tipo=a.tipo,
        descricao=a.descricao,
        valor_aplicado=a.valor_aplicado,
        data_aplicacao=a.data_aplicacao,
        valor_atual=a.valor_atual,
        data_atualizacao_valor=a.data_atualizacao_valor,
        data_vencimento=a.data_vencimento,
        observacao=a.observacao,
        ativa=a.ativa,
        rendimento=a.rendimento,
    )


def _snapshot(a: AplicacaoFinanceira) -> dict[str, object]:
    return {
        "agencia_id": a.agencia_id,
        "instituicao": a.instituicao,
        "tipo": a.tipo,
        "descricao": a.descricao,
        "valor_aplicado": a.valor_aplicado,
        "data_aplicacao": a.data_aplicacao,
        "valor_atual": a.valor_atual,
        "data_vencimento": a.data_vencimento,
        "ativa": a.ativa,
        "deleted_at": a.deleted_at,
    }
