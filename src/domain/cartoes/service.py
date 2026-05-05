"""Serviço de Cartão de Crédito.

Regras de negócio:
- Um cartão pertence a uma empresa (multi-tenant).
- Cada cartão pode ter 0..N faturas; cada fatura é única por competência (mês).
- Lançamentos só podem ser adicionados a faturas com status "aberta" ou "fechada".
- Faturas "pagas" são imutáveis (sem novos lançamentos ou alteração de status).
- Ao adicionar/remover lançamentos, o valor_total da fatura é recalculado.
- Ao associar uma transação bancária a uma fatura, o status muda para "paga".
- Soft delete em cartões, faturas e lançamentos via deleted_at.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import CartaoCredito, FaturaCartao, LancamentoCartao, Transacao
from src.schemas.cartoes import (
    AssociarTransacaoFaturaRequest,
    CartaoCreate,
    CartaoListResponse,
    CartaoResponse,
    CartaoUpdate,
    FaturaCreate,
    FaturaListResponse,
    FaturaResponse,
    FaturaUpdate,
    ImportCSVResponse,
    LancamentoBulkImport,
    LancamentoCreate,
    LancamentoListResponse,
    LancamentoResponse,
)

logger = structlog.get_logger(__name__)


class CartaoService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    # ── Cartões ───────────────────────────────────────────────────────────────

    async def listar_cartoes(self) -> CartaoListResponse:
        rows = (
            await self._db.execute(
                select(CartaoCredito)
                .where(
                    CartaoCredito.empresa_id == self._empresa_id,
                    CartaoCredito.deleted_at.is_(None),
                )
                .order_by(CartaoCredito.nome)
            )
        ).scalars().all()

        items = []
        for c in rows:
            total_fat = (
                await self._db.execute(
                    select(func.count()).where(
                        FaturaCartao.cartao_id == c.id,
                        FaturaCartao.deleted_at.is_(None),
                    )
                )
            ).scalar_one()

            fat_aberta = (
                await self._db.execute(
                    select(FaturaCartao.valor_total).where(
                        FaturaCartao.cartao_id == c.id,
                        FaturaCartao.status == "aberta",
                        FaturaCartao.deleted_at.is_(None),
                    ).order_by(FaturaCartao.competencia.desc()).limit(1)
                )
            ).scalar_one_or_none()

            items.append(
                CartaoResponse(
                    id=c.id,
                    empresa_id=c.empresa_id,
                    nome=c.nome,
                    bandeira=c.bandeira,
                    ultimos_digitos=c.ultimos_digitos,
                    dia_fechamento=c.dia_fechamento,
                    dia_vencimento=c.dia_vencimento,
                    limite=float(c.limite) if c.limite else None,
                    ativo=c.ativo,
                    total_faturas=total_fat,
                    fatura_aberta_valor=float(fat_aberta) if fat_aberta else None,
                )
            )
        return CartaoListResponse(items=items, total=len(items))

    async def criar_cartao(self, data: CartaoCreate) -> CartaoResponse:
        cartao = CartaoCredito(
            id=uuid.uuid4(),
            empresa_id=self._empresa_id,
            nome=data.nome,
            bandeira=data.bandeira,
            ultimos_digitos=data.ultimos_digitos,
            dia_fechamento=data.dia_fechamento,
            dia_vencimento=data.dia_vencimento,
            limite=data.limite,
        )
        self._db.add(cartao)
        await self._db.flush()
        logger.info("cartao.criado", cartao_id=str(cartao.id), nome=cartao.nome)
        return CartaoResponse(
            id=cartao.id, empresa_id=cartao.empresa_id, nome=cartao.nome,
            bandeira=cartao.bandeira, ultimos_digitos=cartao.ultimos_digitos,
            dia_fechamento=cartao.dia_fechamento, dia_vencimento=cartao.dia_vencimento,
            limite=float(cartao.limite) if cartao.limite else None, ativo=cartao.ativo,
        )

    async def atualizar_cartao(self, cartao_id: UUID, data: CartaoUpdate) -> CartaoResponse:
        cartao = await self._get_cartao_or_404(cartao_id)
        if data.nome is not None:
            cartao.nome = data.nome
        if data.dia_fechamento is not None:
            cartao.dia_fechamento = data.dia_fechamento
        if data.dia_vencimento is not None:
            cartao.dia_vencimento = data.dia_vencimento
        if data.limite is not None:
            cartao.limite = data.limite
        if data.ativo is not None:
            cartao.ativo = data.ativo
        await self._db.flush()
        return CartaoResponse(
            id=cartao.id, empresa_id=cartao.empresa_id, nome=cartao.nome,
            bandeira=cartao.bandeira, ultimos_digitos=cartao.ultimos_digitos,
            dia_fechamento=cartao.dia_fechamento, dia_vencimento=cartao.dia_vencimento,
            limite=float(cartao.limite) if cartao.limite else None, ativo=cartao.ativo,
        )

    async def remover_cartao(self, cartao_id: UUID) -> None:
        cartao = await self._get_cartao_or_404(cartao_id)
        # Verifica se tem faturas com saldo
        faturas_abertas = (
            await self._db.execute(
                select(func.count()).where(
                    FaturaCartao.cartao_id == cartao_id,
                    FaturaCartao.status.in_(["aberta", "fechada"]),
                    FaturaCartao.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        if faturas_abertas > 0:
            raise ConflictError(
                message=f"Cartão possui {faturas_abertas} fatura(s) em aberto. Quite-as antes de remover."
            )
        cartao.deleted_at = datetime.now(UTC)
        await self._db.flush()

    # ── Faturas ───────────────────────────────────────────────────────────────

    async def listar_faturas(self, cartao_id: UUID) -> FaturaListResponse:
        await self._get_cartao_or_404(cartao_id)
        rows = (
            await self._db.execute(
                select(FaturaCartao, CartaoCredito)
                .join(CartaoCredito, CartaoCredito.id == FaturaCartao.cartao_id)
                .where(
                    FaturaCartao.cartao_id == cartao_id,
                    FaturaCartao.deleted_at.is_(None),
                )
                .order_by(FaturaCartao.competencia.desc())
            )
        ).all()

        items = []
        for f, c in rows:
            total_lanc = (
                await self._db.execute(
                    select(func.count()).where(
                        LancamentoCartao.fatura_id == f.id,
                        LancamentoCartao.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            items.append(_fatura_to_response(f, c, total_lanc))

        return FaturaListResponse(items=items, total=len(items))

    async def criar_fatura(self, cartao_id: UUID, data: FaturaCreate) -> FaturaResponse:
        cartao = await self._get_cartao_or_404(cartao_id)

        # Unicidade por competência
        existe = (
            await self._db.execute(
                select(FaturaCartao).where(
                    FaturaCartao.cartao_id == cartao_id,
                    FaturaCartao.competencia == data.competencia,
                    FaturaCartao.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existe:
            raise ConflictError(
                message=f"Já existe fatura para a competência {data.competencia} neste cartão."
            )

        fatura = FaturaCartao(
            id=uuid.uuid4(),
            empresa_id=self._empresa_id,
            cartao_id=cartao_id,
            competencia=data.competencia,
            data_fechamento=data.data_fechamento,
            data_vencimento=data.data_vencimento,
            valor_total=0,
            status="aberta",
            observacao=data.observacao,
        )
        self._db.add(fatura)
        await self._db.flush()
        logger.info("fatura.criada", fatura_id=str(fatura.id), competencia=data.competencia)
        return _fatura_to_response(fatura, cartao, 0)

    async def atualizar_fatura(
        self, cartao_id: UUID, fatura_id: UUID, data: FaturaUpdate
    ) -> FaturaResponse:
        fatura, cartao = await self._get_fatura_or_404(cartao_id, fatura_id)
        if fatura.status == "paga" and data.status != "paga":
            raise ValidationError(message="Faturas pagas não podem ser reabertas.")

        if data.status is not None:
            fatura.status = data.status
        if data.data_vencimento is not None:
            fatura.data_vencimento = data.data_vencimento
        if data.observacao is not None:
            fatura.observacao = data.observacao
        await self._db.flush()

        total_lanc = await self._count_lancamentos(fatura_id)
        return _fatura_to_response(fatura, cartao, total_lanc)

    async def associar_transacao(
        self, cartao_id: UUID, fatura_id: UUID, data: AssociarTransacaoFaturaRequest
    ) -> FaturaResponse:
        fatura, cartao = await self._get_fatura_or_404(cartao_id, fatura_id)

        # Valida transação
        transacao = (
            await self._db.execute(
                select(Transacao).where(
                    Transacao.id == data.transacao_id,
                    Transacao.empresa_id == self._empresa_id,
                )
            )
        ).scalar_one_or_none()
        if not transacao:
            raise NotFoundError(message="Transação não encontrada para esta empresa.")

        fatura.transacao_id = data.transacao_id
        fatura.status = "paga"
        await self._db.flush()
        logger.info("fatura.paga", fatura_id=str(fatura_id), transacao_id=str(data.transacao_id))

        total_lanc = await self._count_lancamentos(fatura_id)
        return _fatura_to_response(fatura, cartao, total_lanc)

    async def desassociar_transacao(
        self, cartao_id: UUID, fatura_id: UUID
    ) -> FaturaResponse:
        fatura, cartao = await self._get_fatura_or_404(cartao_id, fatura_id)
        fatura.transacao_id = None
        fatura.status = "fechada"
        await self._db.flush()
        total_lanc = await self._count_lancamentos(fatura_id)
        return _fatura_to_response(fatura, cartao, total_lanc)

    # ── Lançamentos ───────────────────────────────────────────────────────────

    async def listar_lancamentos(
        self, cartao_id: UUID, fatura_id: UUID
    ) -> LancamentoListResponse:
        fatura, _ = await self._get_fatura_or_404(cartao_id, fatura_id)

        rows = (
            await self._db.execute(
                select(LancamentoCartao)
                .where(
                    LancamentoCartao.fatura_id == fatura_id,
                    LancamentoCartao.deleted_at.is_(None),
                )
                .order_by(LancamentoCartao.data_compra)
            )
        ).scalars().all()

        valor_total = sum(float(r.valor) for r in rows)
        items = [_lancamento_to_response(r) for r in rows]
        return LancamentoListResponse(items=items, total=len(items), valor_total=valor_total)

    async def adicionar_lancamento(
        self, cartao_id: UUID, fatura_id: UUID, data: LancamentoCreate
    ) -> LancamentoResponse:
        fatura, _ = await self._get_fatura_or_404(cartao_id, fatura_id)
        if fatura.status == "paga":
            raise ValidationError(message="Não é possível adicionar lançamentos a uma fatura paga.")

        lanc = LancamentoCartao(
            id=uuid.uuid4(),
            empresa_id=self._empresa_id,
            fatura_id=fatura_id,
            data_compra=data.data_compra,
            descricao=data.descricao,
            valor=data.valor,
            conta_id=data.conta_id,
            parcela_atual=data.parcela_atual,
            parcela_total=data.parcela_total,
        )
        self._db.add(lanc)
        await self._db.flush()
        await self._recalcular_total(fatura)
        return _lancamento_to_response(lanc)

    async def importar_csv(
        self, cartao_id: UUID, fatura_id: UUID, conteudo: bytes
    ) -> ImportCSVResponse:
        fatura, _ = await self._get_fatura_or_404(cartao_id, fatura_id)
        if fatura.status == "paga":
            raise ValidationError(message="Não é possível importar lançamentos em fatura paga.")

        texto = conteudo.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(texto))

        # Aceita cabeçalhos em PT e EN, case-insensitive
        importados = 0
        erros: list[str] = []

        for i, row in enumerate(reader, start=2):
            linha_num = i
            row_lower = {k.lower().strip(): v.strip() for k, v in row.items() if k}
            try:
                # data_compra
                data_raw = (
                    row_lower.get("data_compra")
                    or row_lower.get("data")
                    or row_lower.get("date")
                    or ""
                ).strip()
                if not data_raw:
                    erros.append(f"Linha {linha_num}: campo 'data_compra' ausente.")
                    continue
                data_compra = _parse_data(data_raw)

                # descricao
                descricao = (
                    row_lower.get("descricao")
                    or row_lower.get("description")
                    or row_lower.get("historico")
                    or ""
                ).strip()
                if not descricao:
                    erros.append(f"Linha {linha_num}: campo 'descricao' ausente.")
                    continue

                # valor
                valor_raw = (
                    row_lower.get("valor")
                    or row_lower.get("value")
                    or row_lower.get("amount")
                    or ""
                ).strip()
                if not valor_raw:
                    erros.append(f"Linha {linha_num}: campo 'valor' ausente.")
                    continue
                valor = _parse_valor(valor_raw)
                if valor <= 0:
                    erros.append(f"Linha {linha_num}: valor deve ser positivo ({valor_raw}).")
                    continue

                lanc = LancamentoCartao(
                    id=uuid.uuid4(),
                    empresa_id=self._empresa_id,
                    fatura_id=fatura_id,
                    data_compra=data_compra,
                    descricao=descricao,
                    valor=valor,
                )
                self._db.add(lanc)
                importados += 1

            except Exception as exc:
                erros.append(f"Linha {linha_num}: {exc}")

        if importados > 0:
            await self._db.flush()
            await self._recalcular_total(fatura)

        logger.info(
            "fatura.csv_importado",
            fatura_id=str(fatura_id),
            importados=importados,
            erros=len(erros),
        )
        return ImportCSVResponse(importados=importados, erros=erros)

    async def remover_lancamento(
        self, cartao_id: UUID, fatura_id: UUID, lancamento_id: UUID
    ) -> None:
        fatura, _ = await self._get_fatura_or_404(cartao_id, fatura_id)
        if fatura.status == "paga":
            raise ValidationError(message="Não é possível remover lançamentos de uma fatura paga.")

        lanc = (
            await self._db.execute(
                select(LancamentoCartao).where(
                    LancamentoCartao.id == lancamento_id,
                    LancamentoCartao.fatura_id == fatura_id,
                    LancamentoCartao.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not lanc:
            raise NotFoundError(message="Lançamento não encontrado.")

        lanc.deleted_at = datetime.now(UTC)
        await self._db.flush()
        await self._recalcular_total(fatura)

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _get_cartao_or_404(self, cartao_id: UUID) -> CartaoCredito:
        c = (
            await self._db.execute(
                select(CartaoCredito).where(
                    CartaoCredito.id == cartao_id,
                    CartaoCredito.empresa_id == self._empresa_id,
                    CartaoCredito.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not c:
            raise NotFoundError(message="Cartão não encontrado.")
        return c

    async def _get_fatura_or_404(
        self, cartao_id: UUID, fatura_id: UUID
    ) -> tuple[FaturaCartao, CartaoCredito]:
        row = (
            await self._db.execute(
                select(FaturaCartao, CartaoCredito)
                .join(CartaoCredito, CartaoCredito.id == FaturaCartao.cartao_id)
                .where(
                    FaturaCartao.id == fatura_id,
                    FaturaCartao.cartao_id == cartao_id,
                    FaturaCartao.empresa_id == self._empresa_id,
                    FaturaCartao.deleted_at.is_(None),
                )
            )
        ).one_or_none()
        if not row:
            raise NotFoundError(message="Fatura não encontrada.")
        return row

    async def _recalcular_total(self, fatura: FaturaCartao) -> None:
        total = (
            await self._db.execute(
                select(func.coalesce(func.sum(LancamentoCartao.valor), 0)).where(
                    LancamentoCartao.fatura_id == fatura.id,
                    LancamentoCartao.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        fatura.valor_total = total
        await self._db.flush()

    async def _count_lancamentos(self, fatura_id: UUID) -> int:
        return (
            await self._db.execute(
                select(func.count()).where(
                    LancamentoCartao.fatura_id == fatura_id,
                    LancamentoCartao.deleted_at.is_(None),
                )
            )
        ).scalar_one()


# ── helpers de conversão ──────────────────────────────────────────────────────

def _fatura_to_response(
    f: FaturaCartao, c: CartaoCredito, total_lancamentos: int
) -> FaturaResponse:
    return FaturaResponse(
        id=f.id,
        empresa_id=f.empresa_id,
        cartao_id=f.cartao_id,
        cartao_nome=c.nome,
        cartao_bandeira=c.bandeira,
        cartao_digitos=c.ultimos_digitos,
        competencia=f.competencia,
        data_fechamento=f.data_fechamento,
        data_vencimento=f.data_vencimento,
        valor_total=float(f.valor_total),
        status=f.status,
        transacao_id=f.transacao_id,
        observacao=f.observacao,
        total_lancamentos=total_lancamentos,
    )


def _lancamento_to_response(l: LancamentoCartao) -> LancamentoResponse:
    return LancamentoResponse(
        id=l.id,
        fatura_id=l.fatura_id,
        empresa_id=l.empresa_id,
        data_compra=l.data_compra,
        descricao=l.descricao,
        valor=float(l.valor),
        conta_id=l.conta_id,
        parcela_atual=l.parcela_atual,
        parcela_total=l.parcela_total,
    )


def _parse_data(s: str) -> datetime:
    """Tenta vários formatos de data: dd/mm/yyyy, yyyy-mm-dd, dd-mm-yyyy."""
    from datetime import timezone
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Data inválida: '{s}'. Use dd/mm/yyyy ou yyyy-mm-dd.")


def _parse_valor(s: str) -> float:
    """Trata separadores BR (1.234,56) e EN (1,234.56)."""
    s = s.replace(" ", "").replace("R$", "").strip()
    # Se contém vírgula como decimal (BR): 1.234,56
    if "," in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    return float(s)
