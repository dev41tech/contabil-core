"""Serviço de Contrapartes (fornecedor/cliente identificado por CPF/CNPJ).

Regras de negócio:
- `documento` único por empresa entre contrapartes ativas (permite recriar
  se a anterior foi desativada).
- `conta_contabil_id` precisa existir e pertencer à mesma empresa.
- Toda contraparte criada por este cadastro é considerada confirmada
  (`confirmado_em`/`confirmado_por` preenchidos automaticamente) — ainda não
  existe nenhum fluxo de sugestão automática que precise do estado
  "pendente de revisão"; isso é trabalho de uma entrega futura (backfill /
  resolução automática pelo NEO), quando `origem` passa a variar.
- Desativar é preferível a remover — mantém o histórico de vinculação.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.core.cnpj import somente_digitos
from src.core.context import get_user_id
from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import Contraparte, PlanoConta
from src.domain.auditoria import registrar_auditoria
from src.schemas.contrapartes import (
    ContraparteCreate,
    ContraparteListResponse,
    ContraparteResponse,
    ContraparteUpdate,
)

logger = structlog.get_logger(__name__)

_NAO_ENCONTRADA = "Contraparte não encontrada."


class ContraparteService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def listar(
        self,
        termo: str | None = None,
        tipo: str | None = None,
        apenas_ativas: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> ContraparteListResponse:
        q = (
            select(Contraparte)
            .options(joinedload(Contraparte.conta_contabil))
            .where(
                Contraparte.empresa_id == self._empresa_id,
                Contraparte.deleted_at.is_(None),
            )
        )
        count_q = select(func.count(Contraparte.id)).where(
            Contraparte.empresa_id == self._empresa_id,
            Contraparte.deleted_at.is_(None),
        )
        if apenas_ativas:
            q = q.where(Contraparte.ativa == True)
            count_q = count_q.where(Contraparte.ativa == True)
        if tipo:
            q = q.where(Contraparte.tipo == tipo)
            count_q = count_q.where(Contraparte.tipo == tipo)
        if termo:
            filtro = self._filtro_busca(termo)
            q = q.where(filtro)
            count_q = count_q.where(filtro)

        total = (await self._db.execute(count_q)).scalar_one()
        rows = (
            await self._db.execute(
                q.order_by(Contraparte.razao_social)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        return ContraparteListResponse(
            items=[_to_response(c) for c in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def obter(self, contraparte_id: UUID) -> ContraparteResponse:
        contraparte = await self._get_or_404(contraparte_id)
        return _to_response(contraparte)

    async def criar(self, data: ContraparteCreate) -> ContraparteResponse:
        conta = await self._validar_conta(data.conta_contabil_id)
        await self._assert_documento_livre(data.documento)

        contraparte = Contraparte(
            empresa_id=self._empresa_id,
            tipo=data.tipo,
            documento=data.documento,
            razao_social=data.razao_social,
            nome_fantasia=data.nome_fantasia,
            conta_contabil_id=data.conta_contabil_id,
            origem="manual",
            confirmado_em=datetime.now(UTC),
            confirmado_por=_usuario_do_contexto(),
        )
        self._db.add(contraparte)
        await self._db.flush()

        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="contraparte.criada",
            entidade="contraparte",
            entidade_id=contraparte.id,
            dados_depois=_snapshot(contraparte),
        )
        logger.info(
            "contraparte.criada",
            contraparte_id=str(contraparte.id),
            empresa_id=str(self._empresa_id),
        )
        return _to_response(contraparte, conta=conta)

    async def atualizar(
        self, contraparte_id: UUID, data: ContraparteUpdate
    ) -> ContraparteResponse:
        contraparte = await self._get_or_404(contraparte_id)
        antes = _snapshot(contraparte)
        conta_atualizada = None

        if data.tipo is not None:
            contraparte.tipo = data.tipo
        if data.razao_social is not None:
            contraparte.razao_social = data.razao_social
        if data.nome_fantasia is not None:
            contraparte.nome_fantasia = data.nome_fantasia
        if data.conta_contabil_id is not None:
            conta_atualizada = await self._validar_conta(data.conta_contabil_id)
            contraparte.conta_contabil_id = data.conta_contabil_id
        if data.ativa is True and not contraparte.ativa:
            await self._assert_documento_livre(
                contraparte.documento, excluir_id=contraparte.id
            )
        if data.ativa is not None:
            contraparte.ativa = data.ativa

        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="contraparte.atualizada",
            entidade="contraparte",
            entidade_id=contraparte.id,
            dados_antes=antes,
            dados_depois=_snapshot(contraparte),
        )
        logger.info("contraparte.atualizada", contraparte_id=str(contraparte_id))
        return _to_response(contraparte, conta=conta_atualizada)

    async def remover(self, contraparte_id: UUID) -> None:
        """Remove (soft delete) um cadastro — para engano de cadastro, não para desligamento."""
        contraparte = await self._get_or_404(contraparte_id)
        antes = _snapshot(contraparte)

        contraparte.deleted_at = datetime.now(UTC)
        await self._db.flush()
        await registrar_auditoria(
            self._db,
            empresa_id=self._empresa_id,
            acao="contraparte.removida",
            entidade="contraparte",
            entidade_id=contraparte.id,
            dados_antes=antes,
            dados_depois=_snapshot(contraparte),
        )
        logger.info("contraparte.removida", contraparte_id=str(contraparte_id))

    # ── Helpers privados ─────────────────────────────────────────────────────

    async def _get_or_404(self, contraparte_id: UUID) -> Contraparte:
        result = await self._db.execute(
            select(Contraparte)
            .options(joinedload(Contraparte.conta_contabil))
            .where(
                Contraparte.id == contraparte_id,
                Contraparte.empresa_id == self._empresa_id,
                Contraparte.deleted_at.is_(None),
            )
        )
        contraparte = result.scalar_one_or_none()
        if not contraparte:
            raise NotFoundError(message=_NAO_ENCONTRADA)
        return contraparte

    async def _validar_conta(self, conta_id: UUID) -> PlanoConta:
        result = await self._db.execute(
            select(PlanoConta).where(
                PlanoConta.id == conta_id,
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at.is_(None),
            )
        )
        conta = result.scalar_one_or_none()
        if not conta:
            raise ValidationError(message="Conta contábil não encontrada nesta empresa.")
        return conta

    async def _assert_documento_livre(
        self, documento: str, excluir_id: UUID | None = None
    ) -> None:
        q = select(Contraparte.id).where(
            Contraparte.empresa_id == self._empresa_id,
            Contraparte.documento == documento,
            Contraparte.ativa == True,
            Contraparte.deleted_at.is_(None),
        )
        if excluir_id:
            q = q.where(Contraparte.id != excluir_id)
        if (await self._db.execute(q)).scalar_one_or_none():
            raise ConflictError(
                message=(
                    f"Já existe uma contraparte ativa cadastrada com o documento "
                    f"'{documento}'."
                )
            )

    @staticmethod
    def _filtro_busca(termo: str):
        termo_like = f"%{termo.strip()}%"
        condicoes = [
            Contraparte.razao_social.ilike(termo_like),
            Contraparte.nome_fantasia.ilike(termo_like),
        ]
        digitos = somente_digitos(termo)
        if digitos:
            condicoes.append(Contraparte.documento.ilike(f"%{digitos}%"))
        return or_(*condicoes)


def _usuario_do_contexto() -> UUID | None:
    try:
        return UUID(get_user_id())
    except (ValueError, TypeError, AttributeError):
        return None


def _to_response(c: Contraparte, conta: PlanoConta | None = None) -> ContraparteResponse:
    conta = conta or c.conta_contabil
    return ContraparteResponse(
        id=c.id,
        empresa_id=c.empresa_id,
        tipo=c.tipo,
        documento=c.documento,
        razao_social=c.razao_social,
        nome_fantasia=c.nome_fantasia,
        conta_contabil_id=c.conta_contabil_id,
        origem=c.origem,
        confirmado_em=c.confirmado_em,
        ativa=c.ativa,
        conta_codigo=conta.codigo if conta else None,
        conta_descricao=conta.descricao if conta else None,
    )


def _snapshot(c: Contraparte) -> dict[str, object]:
    return {
        "tipo": c.tipo,
        "documento": c.documento,
        "razao_social": c.razao_social,
        "nome_fantasia": c.nome_fantasia,
        "conta_contabil_id": c.conta_contabil_id,
        "ativa": c.ativa,
        "deleted_at": c.deleted_at,
    }
