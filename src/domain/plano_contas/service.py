"""Serviço de Plano de Contas.

Regras de negócio:
- Código único por empresa — não pode repetir dentro do mesmo plano.
- Código do filho deve começar com o código do pai (ex: pai=1, filho=1.1).
- Nível máximo: 5 (ex: 1.1.1.1.1). Além disso fica difícil de operar.
- Não é possível deletar (soft) uma conta que tenha filhos ativos.
- Não é possível deletar uma conta que esteja referenciada em regras ativas.
- Atualizar código não é permitido — código é imutável após criação (muda
  a posição hierárquica e quebraria referências em regras).
- A árvore retorna apenas contas não-deletadas, em ordem por código.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import PlanoConta, Regra
from src.schemas.plano_contas import (
    PlanoContaCreate,
    PlanoContaListResponse,
    PlanoContaNode,
    PlanoContaResponse,
    PlanoContaTreeResponse,
    PlanoContaUpdate,
)

logger = structlog.get_logger(__name__)

_NAO_ENCONTRADA = "Conta contábil não encontrada."
_NIVEL_MAXIMO = 5


class PlanoContaService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    # ── Consultas ─────────────────────────────────────────────────────────────

    async def listar(self) -> PlanoContaListResponse:
        """Lista todas as contas em ordem por código."""
        q = (
            select(PlanoConta)
            .where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
            )
            .order_by(PlanoConta.codigo)
        )
        rows = (await self._db.execute(q)).scalars().all()
        total = len(rows)
        return PlanoContaListResponse(
            items=[PlanoContaResponse.model_validate(r) for r in rows],
            total=total,
        )

    async def arvore(self) -> PlanoContaTreeResponse:
        """Retorna o plano de contas em estrutura de árvore (raízes + filhos aninhados)."""
        # Carrega todas as contas com filhos em uma única query
        q = (
            select(PlanoConta)
            .where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
                PlanoConta.pai_id == None,  # apenas raízes
            )
            .options(selectinload(PlanoConta.filhos).selectinload(PlanoConta.filhos)
                     .selectinload(PlanoConta.filhos).selectinload(PlanoConta.filhos))
            .order_by(PlanoConta.codigo)
        )
        raizes = (await self._db.execute(q)).scalars().all()

        total_q = select(func.count()).where(
            PlanoConta.empresa_id == self._empresa_id,
            PlanoConta.deleted_at == None,
        )
        total = (await self._db.execute(total_q)).scalar_one()

        return PlanoContaTreeResponse(
            tree=[_to_node(r) for r in raizes],
            total=total,
        )

    async def obter(self, conta_id: UUID) -> PlanoContaResponse:
        conta = await self._get_or_404(conta_id)
        return PlanoContaResponse.model_validate(conta)

    # ── Mutações ──────────────────────────────────────────────────────────────

    async def criar(self, data: PlanoContaCreate) -> PlanoContaResponse:
        # Verifica código duplicado
        await self._assert_codigo_livre(data.codigo)

        # Valida pai se informado
        if data.pai_id:
            pai = await self._get_or_404(data.pai_id)
            self._assert_codigo_filho_valido(pai.codigo, data.codigo)
            self._assert_nivel_valido(pai.codigo)

        conta = PlanoConta(
            empresa_id=self._empresa_id,
            conta_numero=data.conta_numero,
            codigo=data.codigo,
            descricao=data.descricao,
            tipo=data.tipo,
            tipo_sa=data.tipo_sa,
            pai_id=data.pai_id,
        )
        self._db.add(conta)
        await self._db.flush()

        # Auto-promove o pai para Sintética quando ganha o primeiro filho
        if data.pai_id:
            pai = await self._get_or_404(data.pai_id)
            if pai.tipo_sa != "S":
                pai.tipo_sa = "S"
                await self._db.flush()

        logger.info(
            "plano_conta.criada",
            conta_id=str(conta.id),
            empresa_id=str(self._empresa_id),
            codigo=conta.codigo,
        )
        return PlanoContaResponse.model_validate(conta)

    async def atualizar(self, conta_id: UUID, data: PlanoContaUpdate) -> PlanoContaResponse:
        conta = await self._get_or_404(conta_id)

        if data.conta_numero is not None:
            conta.conta_numero = data.conta_numero
        if data.codigo is not None and data.codigo != conta.codigo:
            await self._assert_codigo_livre(data.codigo)
            conta.codigo = data.codigo
        if data.descricao is not None:
            conta.descricao = data.descricao
        if data.tipo is not None:
            conta.tipo = data.tipo
        if data.tipo_sa is not None:
            conta.tipo_sa = data.tipo_sa

        await self._db.flush()
        logger.info("plano_conta.atualizada", conta_id=str(conta_id))
        return PlanoContaResponse.model_validate(conta)

    async def remover(self, conta_id: UUID) -> None:
        """Soft delete — bloqueia se houver filhos ou referências em regras."""
        conta = await self._get_or_404(conta_id)

        # Verifica filhos ativos
        filhos_q = select(func.count()).where(
            PlanoConta.pai_id == conta_id,
            PlanoConta.deleted_at == None,
        )
        n_filhos = (await self._db.execute(filhos_q)).scalar_one()
        if n_filhos > 0:
            raise ConflictError(
                message=(
                    f"A conta '{conta.codigo} — {conta.descricao}' possui {n_filhos} "
                    "subconta(s). Remova-as antes de remover a conta pai."
                )
            )

        # Verifica referências em regras
        regras_q = select(func.count()).where(
            Regra.conta_id == conta_id,
            Regra.empresa_id == self._empresa_id,
            Regra.ativa == True,
        )
        n_regras = (await self._db.execute(regras_q)).scalar_one()
        if n_regras > 0:
            raise ConflictError(
                message=(
                    f"A conta '{conta.codigo} — {conta.descricao}' está referenciada "
                    f"em {n_regras} regra(s) ativa(s). Remova as regras antes."
                )
            )

        from datetime import UTC, datetime
        conta.deleted_at = datetime.now(UTC)
        await self._db.flush()
        logger.info("plano_conta.removida", conta_id=str(conta_id), codigo=conta.codigo)

    async def importar_lote(self, rows: list[dict]) -> "ImportacaoPlanoResult":
        """Importa contas em lote. Ignora duplicadas, coleta erros por linha."""
        from src.api.v1.plano_contas import ImportacaoLinhaErro, ImportacaoPlanoResult

        importadas = 0
        duplicadas = 0
        erros: list[ImportacaoLinhaErro] = []

        for row in rows:
            linha = row.get("_linha", 0)
            codigo = row.get("codigo", "").strip()
            descricao = row.get("descricao", "").strip()
            tipo = row.get("tipo", "").strip().lower()
            tipo_sa = (row.get("tipo_sa", "A") or "A").strip().upper()
            conta_num_raw = row.get("conta_numero", row.get("conta", ""))
            conta_numero: int | None = None
            if conta_num_raw:
                try:
                    conta_numero = int(str(conta_num_raw).strip())
                except (ValueError, TypeError):
                    pass

            if not codigo:
                erros.append(ImportacaoLinhaErro(linha=linha, codigo=None, erro="Código ausente."))
                continue
            if not descricao:
                erros.append(ImportacaoLinhaErro(linha=linha, codigo=codigo, erro="Descrição ausente."))
                continue

            try:
                data = PlanoContaCreate(
                    conta_numero=conta_numero,
                    codigo=codigo,
                    descricao=descricao,
                    tipo=tipo or "despesa",
                    tipo_sa=tipo_sa,
                )
            except Exception as e:
                erros.append(ImportacaoLinhaErro(linha=linha, codigo=codigo, erro=str(e)))
                continue

            try:
                await self.criar(data)
                importadas += 1
            except ConflictError:
                duplicadas += 1
            except Exception as e:
                erros.append(ImportacaoLinhaErro(linha=linha, codigo=codigo, erro=str(e)))

        return ImportacaoPlanoResult(importadas=importadas, duplicadas=duplicadas, erros=erros)

    # ── Validações privadas ───────────────────────────────────────────────────

    async def _get_or_404(self, conta_id: UUID) -> PlanoConta:
        result = await self._db.execute(
            select(PlanoConta).where(
                PlanoConta.id == conta_id,
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.deleted_at == None,
            )
        )
        conta = result.scalar_one_or_none()
        if not conta:
            raise NotFoundError(message=_NAO_ENCONTRADA)
        return conta

    async def _assert_codigo_livre(self, codigo: str) -> None:
        existing = await self._db.execute(
            select(PlanoConta).where(
                PlanoConta.empresa_id == self._empresa_id,
                PlanoConta.codigo == codigo,
                PlanoConta.deleted_at == None,
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError(message=f"Já existe uma conta com o código '{codigo}'.")

    def _assert_codigo_filho_valido(self, codigo_pai: str, codigo_filho: str) -> None:
        """O código do filho deve ter o código do pai como prefixo."""
        if not codigo_filho.startswith(codigo_pai + "."):
            raise ValidationError(
                message=(
                    f"O código '{codigo_filho}' não é filho do código '{codigo_pai}'. "
                    f"O código filho deve começar com '{codigo_pai}.' "
                    f"(ex: {codigo_pai}.1)."
                )
            )

    def _assert_nivel_valido(self, codigo_pai: str) -> None:
        nivel_pai = len(codigo_pai.split("."))
        if nivel_pai >= _NIVEL_MAXIMO:
            raise ValidationError(
                message=(
                    f"Nível máximo de hierarquia atingido ({_NIVEL_MAXIMO}). "
                    f"A conta '{codigo_pai}' já está no nível {nivel_pai}."
                )
            )


# ── Helpers de conversão ──────────────────────────────────────────────────────


def _to_node(conta: PlanoConta) -> PlanoContaNode:
    """Converte recursivamente um PlanoConta (com filhos carregados) em PlanoContaNode."""
    filhos_ordenados = sorted(
        [f for f in conta.filhos if f.deleted_at is None],
        key=lambda f: f.codigo,
    )
    return PlanoContaNode(
        id=conta.id,
        empresa_id=conta.empresa_id,
        codigo=conta.codigo,
        descricao=conta.descricao,
        tipo=conta.tipo,
        tipo_sa=conta.tipo_sa,
        pai_id=conta.pai_id,
        nivel=conta.nivel,
        filhos=[_to_node(f) for f in filhos_ordenados],
    )
