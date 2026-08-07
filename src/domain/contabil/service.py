"""Serviço de Registros Contábeis.

Responsabilidades:
- Listar registros com filtros (período, conta, agência, dc).
- Obter registro individual.
- Criação manual de registro (lançamento manual sem transação OFX).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.core.errors import NotFoundError
from src.db.models import RegistroContabil
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
        data_de: date | datetime | None = None,
        data_ate: date | datetime | None = None,
    ) -> RegistroContabilListResponse:
        q = select(RegistroContabil).options(
            joinedload(RegistroContabil.conta),
            joinedload(RegistroContabil.agencia),
        )
        filtros = [
            RegistroContabil.empresa_id == self._empresa_id,
            RegistroContabil.deleted_at.is_(None),
        ]
        if conta_id:
            filtros.append(RegistroContabil.conta_id == conta_id)
        if agencia_id:
            filtros.append(RegistroContabil.agencia_id == agencia_id)
        if dc:
            filtros.append(RegistroContabil.dc == dc.upper())
        if data_de:
            inicio = self._inicio_do_filtro(data_de)
            filtros.append(RegistroContabil.data_lancamento >= inicio)
        if data_ate:
            if isinstance(data_ate, datetime):
                limite = data_ate
                if limite.tzinfo is None:
                    limite = limite.replace(tzinfo=UTC)
                filtros.append(RegistroContabil.data_lancamento <= limite)
            else:
                limite_exclusivo = datetime.combine(
                    data_ate + timedelta(days=1), time.min, tzinfo=UTC
                )
                filtros.append(RegistroContabil.data_lancamento < limite_exclusivo)

        q = q.where(*filtros)
        count_q = select(func.count(RegistroContabil.id)).where(*filtros)
        total = (await self._db.execute(count_q)).scalar_one()

        rows = (
            await self._db.execute(
                q.order_by(RegistroContabil.data_lancamento.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        items = [self._to_response(r) for r in rows]
        return RegistroContabilListResponse(
            items=items, total=total, page=page, page_size=page_size
        )

    async def obter(self, registro_id: UUID) -> RegistroContabilResponse:
        registro = await self._get_or_404(registro_id)
        return self._to_response(registro)

    # ── Helpers

    async def _get_or_404(self, registro_id: UUID) -> RegistroContabil:
        result = await self._db.execute(
            select(RegistroContabil)
            .options(
                joinedload(RegistroContabil.conta),
                joinedload(RegistroContabil.agencia),
            )
            .where(
                RegistroContabil.id == registro_id,
                RegistroContabil.empresa_id == self._empresa_id,
                RegistroContabil.deleted_at.is_(None),
            )
        )
        registro = result.scalar_one_or_none()
        if not registro:
            raise NotFoundError(message="Registro contábil não encontrado.")
        return registro

    def _to_response(self, r: RegistroContabil) -> RegistroContabilResponse:
        conta = r.conta
        agencia = r.agencia
        return RegistroContabilResponse(
            id=r.id,
            empresa_id=r.empresa_id,
            transacao_id=r.transacao_id,
            lancamento_id=r.lancamento_id,
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

    @staticmethod
    def _inicio_do_filtro(valor: date | datetime) -> datetime:
        if isinstance(valor, datetime):
            return valor if valor.tzinfo is not None else valor.replace(tzinfo=UTC)
        return datetime.combine(valor, time.min, tzinfo=UTC)
