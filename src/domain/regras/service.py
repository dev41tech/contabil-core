"""Serviço de Regras de Categorização.

Regras de negócio:
- Unicidade por (empresa_id, agencia_id, historico) — mesmo texto de extrato
  em contas diferentes é permitido, mas na mesma agência não.
- Historico é salvo exatamente como digitado. Para *unicidade* ele é comparado
  em `historico_normalizado` (strip + lower). Para *matching* o NEO usa uma
  forma canônica mais tolerante — sem acento e sem pontuação, ver
  `src.core.texto.normalizar_para_match`. A diferença é deliberada:
  afrouxar a unicidade invalidaria regras que já existem no banco, enquanto
  afrouxar o matching é justamente o que o escritório pediu.
- Desativar é preferível a deletar — regra inativa não é aplicada pelo NEO.
- Conta precisa existir e pertencer à empresa. A agência é OPCIONAL: sem ela a
  regra vale para todos os bancos, e quando informada precisa ser da empresa.
- Regra de agência vence regra global quando as duas casam — ver
  `NeoEngine._carregar_regras`.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import AgenciaBancaria, PlanoConta, Regra
from src.schemas.regras import (
    RegraCreate,
    RegraListResponse,
    RegraResponse,
    RegraUpdate,
)

logger = structlog.get_logger(__name__)


class RegraService:
    """Mantém as regras cadastradas e suas validações de negócio.

    O tipo `manual` existe por compatibilidade com dados e integrações antigas,
    mas não é carregado pelo motor NEO. Classificações feitas pelo contador
    devem usar a associação manual do NEO.
    """

    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def listar(
        self,
        page: int = 1,
        page_size: int = 50,
        apenas_ativas: bool = False,
        agencia_id: UUID | None = None,
    ) -> RegraListResponse:
        q = (
            select(Regra)
            .options(joinedload(Regra.conta), joinedload(Regra.agencia))
            .where(Regra.empresa_id == self._empresa_id)
        )
        count_q = select(func.count(Regra.id)).where(
            Regra.empresa_id == self._empresa_id
        )
        if apenas_ativas:
            q = q.where(Regra.ativa == True)
            count_q = count_q.where(Regra.ativa == True)
        if agencia_id:
            # As globais entram no filtro porque elas VALEM para esta agência.
            # Sem isso o contador filtra por um banco, não vê a regra que está
            # classificando os lançamentos dele, e conclui que ela sumiu.
            do_escopo = or_(
                Regra.agencia_id == agencia_id, Regra.agencia_id.is_(None)
            )
            q = q.where(do_escopo)
            count_q = count_q.where(do_escopo)

        total = (await self._db.execute(count_q)).scalar_one()

        rows = (
            await self._db.execute(
                q.order_by(Regra.historico).offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()

        return RegraListResponse(
            items=[self._to_response(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def obter(self, regra_id: UUID) -> RegraResponse:
        regra = await self._get_or_404(regra_id)
        return self._to_response(regra)

    async def criar(self, data: RegraCreate) -> RegraResponse:
        conta = await self._validar_conta(data.conta_id)
        agencia = await self._validar_agencia(data.agencia_id)
        await self._assert_historico_livre(data.agencia_id, data.historico)

        regra = Regra(
            empresa_id=self._empresa_id,
            conta_id=data.conta_id,
            agencia_id=data.agencia_id,
            descricao=data.descricao,
            historico=data.historico,
            historico_normalizado=self._normalizar_historico(data.historico),
            dc=data.dc,
            tipo=data.tipo,
            manter_historico=data.manter_historico,
        )
        self._db.add(regra)
        await self._db.flush()

        if data.tipo == "manual":
            logger.warning(
                "regra.manual_nao_aplicada_pelo_motor",
                regra_id=str(regra.id),
                empresa_id=str(self._empresa_id),
                aviso="Esta regra manual não será aplicada pelo motor NEO.",
                orientacao="Use a associação manual do NEO para classificar à mão.",
            )

        logger.info(
            "regra.criada",
            regra_id=str(regra.id),
            empresa_id=str(self._empresa_id),
            historico=regra.historico,
        )
        return self._to_response(regra, conta=conta, agencia=agencia)

    async def atualizar(self, regra_id: UUID, data: RegraUpdate) -> RegraResponse:
        regra = await self._get_or_404(regra_id)

        if data.descricao is not None:
            regra.descricao = data.descricao
        if data.dc is not None:
            regra.dc = data.dc
        if data.tipo is not None:
            regra.tipo = data.tipo
        if data.manter_historico is not None:
            regra.manter_historico = data.manter_historico
        if data.ativa is True and not regra.ativa:
            await self._assert_historico_livre(
                regra.agencia_id, regra.historico, excluir_id=regra.id
            )
        if data.ativa is not None:
            regra.ativa = data.ativa

        await self._db.flush()
        logger.info("regra.atualizada", regra_id=str(regra_id))
        return self._to_response(regra)

    async def desativar(self, regra_id: UUID) -> None:
        regra = await self._get_or_404(regra_id)
        regra.ativa = False
        await self._db.flush()
        logger.info("regra.desativada", regra_id=str(regra_id))

    # ── Helpers

    async def _get_or_404(self, regra_id: UUID) -> Regra:
        result = await self._db.execute(
            select(Regra)
            .options(joinedload(Regra.conta), joinedload(Regra.agencia))
            .where(
                Regra.id == regra_id,
                Regra.empresa_id == self._empresa_id,
            )
        )
        regra = result.scalar_one_or_none()
        if not regra:
            raise NotFoundError(message="Regra não encontrada.")
        return regra

    async def _validar_conta(self, conta_id: UUID) -> PlanoConta:
        result = await self._db.execute(
            select(PlanoConta).where(
                PlanoConta.id == conta_id,
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
            )
        )
        conta = result.scalar_one_or_none()
        if not conta:
            raise ValidationError(message="Conta contábil não encontrada nesta empresa.")
        return conta

    async def _validar_agencia(self, agencia_id: UUID | None) -> AgenciaBancaria | None:
        """Sem agência a regra vale para todas — não há o que validar."""
        if agencia_id is None:
            return None
        result = await self._db.execute(
            select(AgenciaBancaria).where(
                AgenciaBancaria.id == agencia_id,
                AgenciaBancaria.empresa_id == self._empresa_id,
                AgenciaBancaria.deleted_at == None,
            )
        )
        agencia = result.scalar_one_or_none()
        if not agencia:
            raise ValidationError(message="Agência bancária não encontrada nesta empresa.")
        return agencia

    async def _assert_historico_livre(
        self, agencia_id: UUID | None, historico: str, excluir_id: UUID | None = None
    ) -> None:
        """Unicidade DENTRO do escopo — o global tem o seu, separado.

        Uma regra global e uma de agência com o mesmo histórico convivem de
        propósito: é o padrão "regra geral mais exceção", e é a agência que
        vence no motor. O que não pode existir são duas no MESMO escopo, que
        disputariam a transação sem critério.
        """
        q = select(Regra.id).where(
            Regra.empresa_id == self._empresa_id,
            Regra.agencia_id.is_(None)
            if agencia_id is None
            else Regra.agencia_id == agencia_id,
            Regra.historico_normalizado == self._normalizar_historico(historico),
            Regra.ativa == True,
            Regra.deleted_at.is_(None),
        )
        if excluir_id:
            q = q.where(Regra.id != excluir_id)
        result = await self._db.execute(q)
        if result.scalar_one_or_none():
            onde = (
                "para todos os bancos" if agencia_id is None else "para esta agência"
            )
            raise ConflictError(
                message=(
                    f"Já existe uma regra ativa com o histórico '{historico}' "
                    f"{onde}."
                )
            )

    @staticmethod
    def _normalizar_historico(historico: str) -> str:
        return historico.strip().lower()

    def _to_response(
        self,
        regra: Regra,
        conta: PlanoConta | None = None,
        agencia: AgenciaBancaria | None = None,
    ) -> RegraResponse:
        conta = conta or regra.conta
        agencia = agencia or regra.agencia
        return RegraResponse(
            id=regra.id,
            empresa_id=regra.empresa_id,
            conta_id=regra.conta_id,
            agencia_id=regra.agencia_id,
            descricao=regra.descricao,
            historico=regra.historico,
            dc=regra.dc,
            tipo=regra.tipo,
            manter_historico=regra.manter_historico,
            ativa=regra.ativa,
            conta_codigo=conta.codigo if conta else None,
            conta_descricao=conta.descricao if conta else None,
            agencia_descricao=agencia.descricao if agencia else None,
        )
