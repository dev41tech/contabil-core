"""Serviço de Cartão de Crédito.

Regras de negócio:
- Um cartão pertence a uma empresa (multi-tenant).
- Cada cartão pode ter 0..N faturas; cada fatura é única por competência (mês).
- Lançamentos só podem ser adicionados a faturas com status "aberta" ou "fechada".
- Faturas "pagas" são imutáveis (sem novos lançamentos ou alteração de status).
- O valor total da fatura é sempre derivado da soma dos lançamentos ativos.
- Ao associar uma transação bancária a uma fatura, o status muda para "paga".
- Soft delete em cartões, faturas e lançamentos via deleted_at.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import (
    CartaoCredito,
    FaturaCartao,
    LancamentoCartao,
    PlanoConta,
    Transacao,
)
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
        cartoes = (
            await self._db.execute(
                select(CartaoCredito)
                .where(
                    CartaoCredito.empresa_id == self._empresa_id,
                    CartaoCredito.deleted_at.is_(None),
                )
                .order_by(CartaoCredito.nome)
            )
        ).scalars().all()

        faturas_por_cartao: dict[UUID, list[tuple[str, str, Decimal]]] = {}
        if cartoes:
            faturas = (
                await self._db.execute(
                    select(
                        FaturaCartao.cartao_id,
                        FaturaCartao.competencia,
                        FaturaCartao.status,
                        func.coalesce(func.sum(LancamentoCartao.valor), 0),
                    )
                    .outerjoin(
                        LancamentoCartao,
                        and_(
                            LancamentoCartao.fatura_id == FaturaCartao.id,
                            LancamentoCartao.deleted_at.is_(None),
                        ),
                    )
                    .where(
                        FaturaCartao.cartao_id.in_([c.id for c in cartoes]),
                        FaturaCartao.deleted_at.is_(None),
                    )
                    .group_by(
                        FaturaCartao.id,
                        FaturaCartao.cartao_id,
                        FaturaCartao.competencia,
                        FaturaCartao.status,
                    )
                    .order_by(FaturaCartao.competencia.desc())
                )
            ).all()
            for cartao_id, competencia, status, valor_total in faturas:
                faturas_por_cartao.setdefault(cartao_id, []).append(
                    (competencia, status, valor_total)
                )

        items = []
        for c in cartoes:
            faturas = faturas_por_cartao.get(c.id, [])
            fat_aberta = next(
                (valor for _, status, valor in faturas if status == "aberta"),
                None,
            )

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
                    total_faturas=len(faturas),
                    fatura_aberta_valor=(
                        float(fat_aberta) if fat_aberta is not None else None
                    ),
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
        cartao = await self._get_cartao_or_404(cartao_id)
        rows = (
            await self._db.execute(
                select(
                    FaturaCartao,
                    func.count(LancamentoCartao.id),
                    func.coalesce(func.sum(LancamentoCartao.valor), 0),
                )
                .outerjoin(
                    LancamentoCartao,
                    and_(
                        LancamentoCartao.fatura_id == FaturaCartao.id,
                        LancamentoCartao.deleted_at.is_(None),
                    ),
                )
                .where(
                    FaturaCartao.cartao_id == cartao_id,
                    FaturaCartao.deleted_at.is_(None),
                )
                .group_by(FaturaCartao.id)
                .order_by(FaturaCartao.competencia.desc())
            )
        ).all()

        items = [
            _fatura_to_response(fatura, cartao, total_lanc, valor_total)
            for fatura, total_lanc, valor_total in rows
        ]

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
        return _fatura_to_response(fatura, cartao, 0, Decimal("0"))

    async def atualizar_fatura(
        self, cartao_id: UUID, fatura_id: UUID, data: FaturaUpdate
    ) -> FaturaResponse:
        fatura, cartao = await self._get_fatura_or_404(
            cartao_id, fatura_id, for_update=True
        )
        if fatura.status == "paga" and data.status != "paga":
            raise ValidationError(message="Faturas pagas não podem ser reabertas.")
        if fatura.status != "paga" and data.status == "paga":
            raise ValidationError(
                message="Uma fatura só pode ser paga pela associação de uma transação válida."
            )

        if data.status is not None:
            fatura.status = data.status
        if data.data_vencimento is not None:
            fatura.data_vencimento = data.data_vencimento
        if data.observacao is not None:
            fatura.observacao = data.observacao
        await self._db.flush()

        total_lanc, valor_total = await self._totais_fatura(fatura_id)
        return _fatura_to_response(fatura, cartao, total_lanc, valor_total)

    async def associar_transacao(
        self, cartao_id: UUID, fatura_id: UUID, data: AssociarTransacaoFaturaRequest
    ) -> FaturaResponse:
        fatura, cartao = await self._get_fatura_or_404(
            cartao_id, fatura_id, for_update=True
        )
        if fatura.transacao_id is not None:
            raise ConflictError(message="Esta fatura já possui uma transação de pagamento.")

        # Valida transação
        transacao = (
            await self._db.execute(
                select(Transacao).where(
                    Transacao.id == data.transacao_id,
                    Transacao.empresa_id == self._empresa_id,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if not transacao:
            raise NotFoundError(message="Transação não encontrada para esta empresa.")

        outra_fatura = (
            await self._db.execute(
                select(FaturaCartao.id).where(
                    FaturaCartao.transacao_id == data.transacao_id,
                    FaturaCartao.id != fatura_id,
                )
            )
        ).scalar_one_or_none()
        if outra_fatura:
            raise ConflictError(message="Esta transação já quitou outra fatura.")

        total_lanc, valor_total = await self._totais_fatura(fatura_id)
        if transacao.dc != "D":
            raise ValidationError(
                message="O pagamento da fatura deve ser uma transação de débito."
            )
        if _valor_monetario(transacao.valor) != _valor_monetario(valor_total):
            raise ValidationError(
                message=(
                    "O valor da transação deve ser igual ao total da fatura "
                    f"({float(valor_total):.2f})."
                )
            )

        fatura.transacao_id = data.transacao_id
        fatura.status = "paga"
        await self._db.flush()
        logger.info("fatura.paga", fatura_id=str(fatura_id), transacao_id=str(data.transacao_id))

        return _fatura_to_response(fatura, cartao, total_lanc, valor_total)

    async def desassociar_transacao(
        self, cartao_id: UUID, fatura_id: UUID
    ) -> FaturaResponse:
        fatura, cartao = await self._get_fatura_or_404(
            cartao_id, fatura_id, for_update=True
        )
        fatura.transacao_id = None
        fatura.status = "fechada"
        await self._db.flush()
        total_lanc, valor_total = await self._totais_fatura(fatura_id)
        return _fatura_to_response(fatura, cartao, total_lanc, valor_total)

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
        fatura, _ = await self._get_fatura_or_404(
            cartao_id, fatura_id, for_update=True
        )
        if fatura.status == "paga":
            raise ValidationError(message="Não é possível adicionar lançamentos a uma fatura paga.")

        await self._validar_conta(data.conta_id)

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
        return _lancamento_to_response(lanc)

    async def importar_csv(
        self, cartao_id: UUID, fatura_id: UUID, conteudo: bytes
    ) -> ImportCSVResponse:
        fatura, _ = await self._get_fatura_or_404(
            cartao_id, fatura_id, for_update=True
        )
        if fatura.status == "paga":
            raise ValidationError(message="Não é possível importar lançamentos em fatura paga.")

        texto = conteudo.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(texto))

        # Aceita cabeçalhos em PT e EN, case-insensitive
        importados = 0
        duplicados = 0
        erros: list[str] = []
        ids_existentes = set(
            (
                await self._db.execute(
                    select(LancamentoCartao.id).where(
                        LancamentoCartao.fatura_id == fatura_id,
                        LancamentoCartao.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        ids_no_arquivo: set[UUID] = set()

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

                identificador = _identificador_compra(
                    row_lower, data_compra, descricao, valor
                )
                lancamento_id = uuid.uuid5(fatura_id, identificador)
                if lancamento_id in ids_existentes or lancamento_id in ids_no_arquivo:
                    duplicados += 1
                    continue
                ids_no_arquivo.add(lancamento_id)

                lanc = LancamentoCartao(
                    id=lancamento_id,
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

        logger.info(
            "fatura.csv_importado",
            fatura_id=str(fatura_id),
            importados=importados,
            duplicados=duplicados,
            erros=len(erros),
        )
        return ImportCSVResponse(
            importados=importados, duplicados=duplicados, erros=erros
        )

    async def remover_lancamento(
        self, cartao_id: UUID, fatura_id: UUID, lancamento_id: UUID
    ) -> None:
        fatura, _ = await self._get_fatura_or_404(
            cartao_id, fatura_id, for_update=True
        )
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
        self, cartao_id: UUID, fatura_id: UUID, *, for_update: bool = False
    ) -> tuple[FaturaCartao, CartaoCredito]:
        stmt = (
            select(FaturaCartao, CartaoCredito)
            .join(CartaoCredito, CartaoCredito.id == FaturaCartao.cartao_id)
            .where(
                FaturaCartao.id == fatura_id,
                FaturaCartao.cartao_id == cartao_id,
                FaturaCartao.empresa_id == self._empresa_id,
                FaturaCartao.deleted_at.is_(None),
            )
        )
        if for_update:
            stmt = stmt.with_for_update(of=FaturaCartao)
        row = (await self._db.execute(stmt)).one_or_none()
        if not row:
            raise NotFoundError(message="Fatura não encontrada.")
        return row

    async def _totais_fatura(self, fatura_id: UUID) -> tuple[int, Decimal]:
        total_lanc, valor_total = (
            await self._db.execute(
                select(
                    func.count(LancamentoCartao.id),
                    func.coalesce(func.sum(LancamentoCartao.valor), 0),
                ).where(
                    LancamentoCartao.fatura_id == fatura_id,
                    LancamentoCartao.deleted_at.is_(None),
                )
            )
        ).one()
        return total_lanc, Decimal(valor_total)

    async def _validar_conta(self, conta_id: UUID | None) -> None:
        if conta_id is None:
            return
        existe = (
            await self._db.execute(
                select(PlanoConta.id).where(
                    PlanoConta.id == conta_id,
                    PlanoConta.empresa_id == self._empresa_id,
                    PlanoConta.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not existe:
            raise NotFoundError(message="Conta contábil não encontrada para esta empresa.")


# ── helpers de conversão ──────────────────────────────────────────────────────

def _fatura_to_response(
    f: FaturaCartao,
    c: CartaoCredito,
    total_lancamentos: int,
    valor_total: Decimal,
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
        valor_total=float(valor_total),
        status=f.status,
        transacao_id=f.transacao_id,
        observacao=f.observacao,
        total_lancamentos=total_lancamentos,
    )


def _valor_monetario(valor: object) -> Decimal:
    return Decimal(str(valor)).quantize(Decimal("0.01"))


def _identificador_compra(
    row: dict[str, str], data_compra: datetime, descricao: str, valor: float
) -> str:
    id_externo = next(
        (
            row.get(campo, "").strip()
            for campo in (
                "id",
                "identificador",
                "transaction_id",
                "transactionid",
                "purchase_id",
                "reference",
                "referencia",
            )
            if row.get(campo, "").strip()
        ),
        None,
    )
    if id_externo:
        base = f"externo:{id_externo}"
    else:
        descricao_normalizada = " ".join(descricao.casefold().split())
        base = "|".join(
            (
                data_compra.astimezone(UTC).isoformat(),
                descricao_normalizada,
                str(_valor_monetario(valor)),
            )
        )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


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
