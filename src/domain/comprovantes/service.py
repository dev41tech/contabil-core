"""Serviço de Comprovantes de Pagamento.

Responsabilidades:
- CRUD de comprovantes de pagamento.
- Associação manual de comprovante a uma transação bancária.
- Desassociação.
- Filtros por agencia_id, transacao_id, status (associado / nao_associado).
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError
from src.db.models import Comprovante, Transacao
from src.schemas.comprovantes import (
    AssociarTransacaoRequest,
    ComprovanteCreate,
    ComprovanteListResponse,
    ComprovanteResponse,
)

logger = structlog.get_logger(__name__)


class ComprovanteService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def listar(
        self,
        page: int = 1,
        page_size: int = 50,
        agencia_id: UUID | None = None,
        transacao_id: UUID | None = None,
        status: str | None = None,
    ) -> ComprovanteListResponse:
        q = select(Comprovante).where(
            Comprovante.empresa_id == self._empresa_id,
            Comprovante.deleted_at == None,
        )
        if agencia_id:
            q = q.where(Comprovante.agencia_id == agencia_id)
        if transacao_id:
            q = q.where(Comprovante.transacao_id == transacao_id)
        if status == "associado":
            q = q.where(Comprovante.transacao_id != None)
        elif status == "nao_associado":
            q = q.where(Comprovante.transacao_id == None)

        count_q = select(func.count()).select_from(q.subquery())
        total = (await self._db.execute(count_q)).scalar_one()

        rows = (
            await self._db.execute(
                q.order_by(Comprovante.data_pagamento.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        return ComprovanteListResponse(
            items=[ComprovanteResponse.from_orm_custom(c) for c in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def obter(self, comprovante_id: UUID) -> ComprovanteResponse:
        comprovante = await self._get_or_404(comprovante_id)
        return ComprovanteResponse.from_orm_custom(comprovante)

    async def criar(self, data: ComprovanteCreate) -> ComprovanteResponse:
        # Valida que a transação pertence à empresa (se informada)
        if data.transacao_id:
            result = await self._db.execute(
                select(Transacao).where(
                    Transacao.id == data.transacao_id,
                    Transacao.empresa_id == self._empresa_id,
                )
            )
            if not result.scalar_one_or_none():
                raise NotFoundError(message="Transação não encontrada nesta empresa.")

        comprovante = Comprovante(
            empresa_id=self._empresa_id,
            agencia_id=data.agencia_id,
            transacao_id=data.transacao_id,
            favorecido=data.favorecido,
            cpf_cnpj=data.cpf_cnpj,
            data_pagamento=data.data_pagamento,
            data_vencimento=data.data_vencimento,
            valor_documento=data.valor_documento,
            valor_pago=data.valor_pago,
            juros=data.juros,
            multa=data.multa,
            desconto=data.desconto,
            observacao=data.observacao,
            arquivo_nome=data.arquivo_nome,
            arquivo_base64=data.arquivo_base64,
        )
        self._db.add(comprovante)
        await self._db.flush()

        logger.info(
            "comprovante.criado",
            comprovante_id=str(comprovante.id),
            empresa_id=str(self._empresa_id),
            valor_pago=str(comprovante.valor_pago),
        )
        return ComprovanteResponse.from_orm_custom(comprovante)

    async def associar_transacao(
        self, comprovante_id: UUID, req: AssociarTransacaoRequest
    ) -> ComprovanteResponse:
        comprovante = await self._get_or_404(comprovante_id)

        if comprovante.transacao_id:
            raise ConflictError(
                message="Este comprovante já está associado a uma transação. Desassocie primeiro."
            )

        # Verifica que a transação pertence à empresa
        result = await self._db.execute(
            select(Transacao).where(
                Transacao.id == req.transacao_id,
                Transacao.empresa_id == self._empresa_id,
            )
        )
        if not result.scalar_one_or_none():
            raise NotFoundError(message="Transação não encontrada nesta empresa.")

        comprovante.transacao_id = req.transacao_id
        await self._db.flush()

        logger.info(
            "comprovante.associado",
            comprovante_id=str(comprovante_id),
            transacao_id=str(req.transacao_id),
        )
        return ComprovanteResponse.from_orm_custom(comprovante)

    async def desassociar_transacao(self, comprovante_id: UUID) -> ComprovanteResponse:
        comprovante = await self._get_or_404(comprovante_id)

        if not comprovante.transacao_id:
            raise ConflictError(message="Comprovante não está associado a nenhuma transação.")

        comprovante.transacao_id = None
        await self._db.flush()

        logger.info("comprovante.desassociado", comprovante_id=str(comprovante_id))
        return ComprovanteResponse.from_orm_custom(comprovante)

    async def deletar(self, comprovante_id: UUID) -> None:
        from datetime import UTC, datetime

        comprovante = await self._get_or_404(comprovante_id)
        comprovante.deleted_at = datetime.now(UTC)
        await self._db.flush()

        logger.info("comprovante.deletado", comprovante_id=str(comprovante_id))

    async def _get_or_404(self, comprovante_id: UUID) -> Comprovante:
        result = await self._db.execute(
            select(Comprovante).where(
                Comprovante.id == comprovante_id,
                Comprovante.empresa_id == self._empresa_id,
                Comprovante.deleted_at == None,
            )
        )
        comprovante = result.scalar_one_or_none()
        if not comprovante:
            raise NotFoundError(message="Comprovante não encontrado.")
        return comprovante
