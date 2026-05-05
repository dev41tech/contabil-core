"""Serviço de Registros Contábeis.

Responsabilidades:
- Listar registros com filtros (período, conta, agência, dc).
- Obter registro individual.
- Criação manual de registro (lançamento manual sem transação OFX).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError, ValidationError
from src.db.models import AgenciaBancaria, PlanoConta, RegistroContabil
from src.schemas.contabil import (
    RegistroContabilListResponse,
    RegistroContabilResponse,
)

logger = structlog.get_logger(__name__)


class ContabilService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def listar(
        self,
        page: int = 1,
        page_size: int = 50,
        conta_id: UUID | None = None,
        agencia_id: UUID | None = None,
        dc: str | None = None,
        data_de: datetime | None = None,
        data_ate: datetime | None = None,
    ) -> RegistroContabilListResponse:
        q = select(RegistroContabil).where(
            RegistroContabil.empresa_id == self._empresa_id
        )
        if conta_id:
            q = q.where(RegistroContabil.conta_id == conta_id)
        if agencia_id:
            q = q.where(RegistroContabil.agencia_id == agencia_id)
        if dc:
            q = q.where(RegistroContabil.dc == dc.upper())
        if data_de:
            q = q.where(RegistroContabil.data_lancamento >= data_de)
        if data_ate:
            q = q.where(RegistroContabil.data_lancamento <= data_ate)

        count_q = select(func.count()).select_from(q.subquery())
        total = (await self._db.execute(count_q)).scalar_one()

        rows = (
            await self._db.execute(
                q.order_by(RegistroContabil.data_lancamento.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        items = [await self._to_response(r) for r in rows]
        return RegistroContabilListResponse(
            items=items, total=total, page=page, page_size=page_size
        )

    async def obter(self, registro_id: UUID) -> RegistroContabilResponse:
        registro = await self._get_or_404(registro_id)
        return await self._to_response(registro)

    # ── Helpers

    async def _get_or_404(self, registro_id: UUID) -> RegistroContabil:
        result = await self._db.execute(
            select(RegistroContabil).where(
                RegistroContabil.id == registro_id,
                RegistroContabil.empresa_id == self._empresa_id,
            )
        )
        registro = result.scalar_one_or_none()
        if not registro:
            raise NotFoundError(message="Registro contábil não encontrado.")
        return registro

    async def _to_response(self, r: RegistroContabil) -> RegistroContabilResponse:
        conta = await self._db.get(PlanoConta, r.conta_id)
        agencia = await self._db.get(AgenciaBancaria, r.agencia_id)
        return RegistroContabilResponse(
            id=r.id,
            empresa_id=r.empresa_id,
            transacao_id=r.transacao_id,
            conta_id=r.conta_id,
            agencia_id=r.agencia_id,
            descricao=r.descricao,
            historico=r.historico,
            historico_extrato=r.historico_extrato,
            dc=r.dc,
            tipo_regra=r.tipo_regra,
            valor=float(r.valor),
            data_lancamento=r.data_lancamento,
            conta_codigo=conta.codigo if conta else None,
            conta_descricao=conta.descricao if conta else None,
            agencia_descricao=agencia.descricao if agencia else None,
        )
