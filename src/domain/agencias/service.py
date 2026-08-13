"""Serviço de agências bancárias.

Regras de negócio:
- Uma empresa pode ter múltiplas contas bancárias.
- Conta duplicada = mesmo (empresa_id, banco_sigla, agencia, numero).
- Desativar não deleta — a conta pode ter histórico de transações vinculado.
- Reativar é permitido (ativa=True via update).
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.core.errors import ConflictError, NotFoundError
from src.db.models import AgenciaBancaria, PlanoConta
from src.schemas.agencias import (
    AgenciaCreate,
    AgenciaListResponse,
    AgenciaResponse,
    AgenciaUpdate,
)

logger = structlog.get_logger(__name__)

_NAO_ENCONTRADA = "Agência bancária não encontrada."


class AgenciaService:
    def __init__(self, db: AsyncSession, empresa_id: UUID, tenant_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id
        self._tenant_id = tenant_id

    async def listar(self, apenas_ativas: bool = False) -> AgenciaListResponse:
        q = select(AgenciaBancaria).options(
            joinedload(AgenciaBancaria.conta_contabil)
        ).where(
            AgenciaBancaria.empresa_id == self._empresa_id,
            AgenciaBancaria.deleted_at == None,
        )
        if apenas_ativas:
            q = q.where(AgenciaBancaria.ativa == True)

        q = q.order_by(AgenciaBancaria.banco_sigla, AgenciaBancaria.agencia)

        rows = (await self._db.execute(q)).scalars().all()

        # total sempre reflete todas (ativas + inativas)
        total_q = select(func.count()).where(
            AgenciaBancaria.empresa_id == self._empresa_id,
            AgenciaBancaria.deleted_at == None,
        )
        total = (await self._db.execute(total_q)).scalar_one()

        return AgenciaListResponse(
            items=[self._to_response(a) for a in rows],
            total=total,
        )

    async def obter(self, agencia_id: UUID) -> AgenciaResponse:
        agencia = await self._get_or_404(agencia_id)
        return self._to_response(agencia)

    async def criar(self, data: AgenciaCreate) -> AgenciaResponse:
        await self._check_duplicata(data.banco_sigla, data.agencia, data.numero)

        agencia = AgenciaBancaria(
            empresa_id=self._empresa_id,
            banco_sigla=data.banco_sigla,
            agencia=data.agencia,
            numero=data.numero,
            digito=data.digito,
        )
        self._db.add(agencia)
        await self._db.flush()

        logger.info(
            "agencia.criada",
            agencia_id=str(agencia.id),
            empresa_id=str(self._empresa_id),
            banco=agencia.banco_sigla,
            descricao=agencia.descricao,
        )
        return self._to_response(agencia)

    async def atualizar(self, agencia_id: UUID, data: AgenciaUpdate) -> AgenciaResponse:
        agencia = await self._get_or_404(agencia_id)

        # Se mudou banco/agência/numero, verifica duplicata
        novo_banco = data.banco_sigla or agencia.banco_sigla
        nova_agencia = data.agencia or agencia.agencia
        novo_numero = data.numero or agencia.numero

        mudou_identificador = (
            novo_banco != agencia.banco_sigla
            or nova_agencia != agencia.agencia
            or novo_numero != agencia.numero
        )
        if mudou_identificador:
            await self._check_duplicata(
                novo_banco, nova_agencia, novo_numero, excluir_id=agencia_id
            )

        if data.banco_sigla is not None:
            agencia.banco_sigla = data.banco_sigla
        if data.agencia is not None:
            agencia.agencia = data.agencia
        if data.numero is not None:
            agencia.numero = data.numero
        if "digito" in data.model_fields_set:
            agencia.digito = data.digito
        if data.ativa is not None:
            agencia.ativa = data.ativa
        if "conta_contabil_id" in data.model_fields_set:
            await self._vincular_conta_contabil(agencia, data.conta_contabil_id)

        try:
            await self._db.flush()
        except IntegrityError as exc:
            raise ConflictError(
                message="Esta conta do Plano de Contas já está vinculada a outra agência bancária."
            ) from exc

        logger.info("agencia.atualizada", agencia_id=str(agencia_id))
        await self._db.refresh(agencia, attribute_names=["conta_contabil"])
        return self._to_response(agencia)

    async def desativar(self, agencia_id: UUID) -> None:
        """Desativa a agência sem deletar — histórico de transações é preservado."""
        agencia = await self._get_or_404(agencia_id)

        if not agencia.ativa:
            # Idempotente: desativar algo já inativo não é erro
            return

        agencia.ativa = False
        await self._db.flush()
        logger.info("agencia.desativada", agencia_id=str(agencia_id))

    # ── Helpers privados

    def _to_response(self, agencia: AgenciaBancaria) -> AgenciaResponse:
        resp = AgenciaResponse.model_validate(agencia)
        if agencia.conta_contabil is not None:
            resp.conta_contabil_codigo = agencia.conta_contabil.codigo
            resp.conta_contabil_descricao = agencia.conta_contabil.descricao
        return resp

    async def _vincular_conta_contabil(
        self, agencia: AgenciaBancaria, conta_contabil_id: UUID | None
    ) -> None:
        if conta_contabil_id is None:
            agencia.conta_contabil_id = None
            return

        conta = (
            await self._db.execute(
                select(PlanoConta).where(
                    PlanoConta.id == conta_contabil_id,
                    PlanoConta.empresa_id == self._empresa_id,
                    PlanoConta.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conta is None:
            raise NotFoundError(
                message="Conta do Plano de Contas não encontrada nesta empresa."
            )
        agencia.conta_contabil_id = conta.id

    async def _get_or_404(self, agencia_id: UUID) -> AgenciaBancaria:
        result = await self._db.execute(
            select(AgenciaBancaria)
            .options(joinedload(AgenciaBancaria.conta_contabil))
            .where(
                AgenciaBancaria.id == agencia_id,
                AgenciaBancaria.empresa_id == self._empresa_id,
                AgenciaBancaria.deleted_at == None,
            )
        )
        agencia = result.scalar_one_or_none()
        if not agencia:
            raise NotFoundError(message=_NAO_ENCONTRADA)
        return agencia

    async def _check_duplicata(
        self,
        banco_sigla: str,
        agencia: str,
        numero: str,
        excluir_id: UUID | None = None,
    ) -> None:
        q = select(AgenciaBancaria).where(
            AgenciaBancaria.empresa_id == self._empresa_id,
            AgenciaBancaria.banco_sigla == banco_sigla,
            AgenciaBancaria.agencia == agencia,
            AgenciaBancaria.numero == numero,
            AgenciaBancaria.deleted_at == None,
        )
        if excluir_id:
            q = q.where(AgenciaBancaria.id != excluir_id)

        existing = (await self._db.execute(q)).scalar_one_or_none()
        if existing:
            raise ConflictError(
                message=(
                    f"Já existe uma conta {banco_sigla} ag.{agencia} c/c {numero} "
                    "cadastrada para esta empresa."
                )
            )
