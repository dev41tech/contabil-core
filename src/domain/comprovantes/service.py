"""Serviço de Comprovantes de Pagamento.

Responsabilidades:
- CRUD de comprovantes de pagamento.
- Associação manual de comprovante a uma transação bancária.
- Desassociação.
- Filtros por agencia_id, transacao_id, status (associado / nao_associado).
- Barrar o mesmo comprovante enviado duas vezes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError
from src.db.models import AgenciaBancaria, Comprovante, Transacao
from src.schemas.comprovantes import (
    AssociarTransacaoRequest,
    ComprovanteCreate,
    ComprovanteListResponse,
    ComprovanteResponse,
)

logger = structlog.get_logger(__name__)

# Códigos que o front usa para decidir o que fazer, e não só o que mostrar: o
# arquivo repetido é pulado da fila, o possível duplicado pede confirmação.
CODIGO_JA_IMPORTADO = "COMPROVANTE_JA_IMPORTADO"
CODIGO_POSSIVEL_DUPLICADO = "COMPROVANTE_POSSIVEL_DUPLICADO"


def hash_do_arquivo(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


def _hash_do_base64(texto: str | None) -> str | None:
    """Hash dos bytes, e não do texto: dois base64 do mesmo arquivo podem diferir
    em quebra de linha, e o arquivo é o mesmo."""
    if not texto:
        return None
    try:
        return hash_do_arquivo(base64.b64decode(texto))
    except (binascii.Error, ValueError):
        return None


def _so_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


def _nome_normalizado(texto: str | None) -> str:
    return re.sub(r"\s+", " ", texto or "").strip().casefold()


def _brl(valor: Decimal) -> str:
    inteiro, _, centavos = f"{valor:,.2f}".partition(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"


def _resumo(comprovante: Comprovante) -> dict:
    """O que o contador precisa ver para reconhecer o registro que já existe."""
    return {
        "comprovante_id": str(comprovante.id),
        "favorecido": comprovante.favorecido,
        "valor_pago": str(comprovante.valor_pago),
        "data_pagamento": (
            comprovante.data_pagamento.isoformat() if comprovante.data_pagamento else None
        ),
        "arquivo_nome": comprovante.arquivo_nome,
        "associado": comprovante.transacao_id is not None,
    }


def _descrever(comprovante: Comprovante) -> str:
    partes = [f"comprovante de {_brl(comprovante.valor_pago)}"]
    if comprovante.favorecido:
        partes.append(f"para {comprovante.favorecido}")
    if comprovante.data_pagamento:
        partes.append(f"pago em {comprovante.data_pagamento.strftime('%d/%m/%Y')}")
    descricao = " ".join(partes)
    if comprovante.arquivo_nome:
        descricao += f" (arquivo {comprovante.arquivo_nome})"
    return descricao


def erro_ja_importado(existente: Comprovante) -> ConflictError:
    return ConflictError(
        message=f"Este arquivo já foi importado: {_descrever(existente)}.",
        code=CODIGO_JA_IMPORTADO,
        details=_resumo(existente),
    )


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
                q.add_columns(Transacao)
                .outerjoin(
                    Transacao,
                    and_(
                        Comprovante.transacao_id == Transacao.id,
                        Transacao.empresa_id == self._empresa_id,
                    ),
                )
                .order_by(Comprovante.data_pagamento.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        return ComprovanteListResponse(
            items=[
                ComprovanteResponse.from_orm_custom(comprovante, transacao)
                for comprovante, transacao in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def obter(self, comprovante_id: UUID) -> ComprovanteResponse:
        comprovante, transacao = await self._get_com_transacao_or_404(comprovante_id)
        return ComprovanteResponse.from_orm_custom(comprovante, transacao)

    async def criar(self, data: ComprovanteCreate) -> ComprovanteResponse:
        if data.agencia_id:
            agencia = (
                await self._db.execute(
                    select(AgenciaBancaria.id).where(
                        AgenciaBancaria.id == data.agencia_id,
                        AgenciaBancaria.empresa_id == self._empresa_id,
                        AgenciaBancaria.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if not agencia:
                raise NotFoundError(message="Agência bancária não encontrada nesta empresa.")

        # Valida que a transação pertence à empresa (se informada)
        transacao = None
        if data.transacao_id:
            result = await self._db.execute(
                select(Transacao).where(
                    Transacao.id == data.transacao_id,
                    Transacao.empresa_id == self._empresa_id,
                )
            )
            transacao = result.scalar_one_or_none()
            if not transacao:
                raise NotFoundError(message="Transação não encontrada nesta empresa.")

        # O mesmo arquivo é sempre o mesmo comprovante — sem confirmação que
        # libere. Já o mesmo valor, dia e favorecido com arquivo diferente pode
        # ser um segundo pagamento de verdade, e aí quem decide é o contador.
        arquivo_sha256 = _hash_do_base64(data.arquivo_base64)
        if arquivo_sha256:
            existente = await self.buscar_por_arquivo(arquivo_sha256)
            if existente:
                raise erro_ja_importado(existente)
        if not data.permitir_duplicado:
            parecido = await self._mesmo_pagamento(data)
            if parecido:
                raise ConflictError(
                    message=(
                        f"Já existe um {_descrever(parecido)}. Se forem dois "
                        "pagamentos diferentes, confirme para salvar mesmo assim."
                    ),
                    code=CODIGO_POSSIVEL_DUPLICADO,
                    details=_resumo(parecido),
                )

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
            arquivo_sha256=arquivo_sha256,
        )
        self._db.add(comprovante)
        await self._db.flush()

        logger.info(
            "comprovante.criado",
            comprovante_id=str(comprovante.id),
            empresa_id=str(self._empresa_id),
            valor_pago=str(comprovante.valor_pago),
        )
        return ComprovanteResponse.from_orm_custom(comprovante, transacao)

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
        transacao = result.scalar_one_or_none()
        if not transacao:
            raise NotFoundError(message="Transação não encontrada nesta empresa.")

        comprovante.transacao_id = req.transacao_id
        await self._db.flush()

        logger.info(
            "comprovante.associado",
            comprovante_id=str(comprovante_id),
            transacao_id=str(req.transacao_id),
        )
        return ComprovanteResponse.from_orm_custom(comprovante, transacao)

    async def desassociar_transacao(self, comprovante_id: UUID) -> ComprovanteResponse:
        comprovante = await self._get_or_404(comprovante_id)

        if not comprovante.transacao_id:
            raise ConflictError(message="Comprovante não está associado a nenhuma transação.")

        comprovante.transacao_id = None
        await self._db.flush()

        logger.info("comprovante.desassociado", comprovante_id=str(comprovante_id))
        return ComprovanteResponse.from_orm_custom(comprovante)

    async def deletar(self, comprovante_id: UUID) -> None:
        comprovante = await self._get_or_404(comprovante_id)
        comprovante.deleted_at = datetime.now(UTC)
        await self._db.flush()

        logger.info("comprovante.deletado", comprovante_id=str(comprovante_id))

    async def buscar_por_arquivo(self, arquivo_sha256: str) -> Comprovante | None:
        """Comprovante vivo desta empresa com exatamente o mesmo arquivo.

        Excluído não conta: excluir é justamente como o contador corrige uma
        importação errada, e o arquivo certo precisa poder entrar depois.
        """
        return (
            await self._db.execute(
                select(Comprovante)
                .where(
                    Comprovante.empresa_id == self._empresa_id,
                    Comprovante.arquivo_sha256 == arquivo_sha256,
                    Comprovante.deleted_at.is_(None),
                )
                .order_by(Comprovante.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _mesmo_pagamento(self, data: ComprovanteCreate) -> Comprovante | None:
        """Comprovante de mesmo valor, mesmo dia e mesmo favorecido.

        Pega o reenvio que o hash não pega: o mesmo comprovante baixado de novo
        do app do banco sai com bytes diferentes.

        Exige uma identidade em comum — documento, ou nome quando falta
        documento. Valor e dia sozinhos casariam dois boletos de R$ 500,00 de
        fornecedores diferentes, e barrar isso trava trabalho legítimo.
        """
        if data.data_pagamento is None:
            return None
        documento = _so_digitos(data.cpf_cnpj)
        nome = _nome_normalizado(data.favorecido)
        if not documento and not nome:
            return None

        pagamento = data.data_pagamento
        if pagamento.tzinfo is None:
            pagamento = pagamento.replace(tzinfo=UTC)
        dia = pagamento.astimezone(UTC).date()
        inicio = datetime(dia.year, dia.month, dia.day, tzinfo=UTC)

        candidatos = (
            await self._db.execute(
                select(Comprovante)
                .where(
                    Comprovante.empresa_id == self._empresa_id,
                    Comprovante.deleted_at.is_(None),
                    Comprovante.valor_pago == data.valor_pago,
                    Comprovante.data_pagamento >= inicio,
                    Comprovante.data_pagamento < inicio + timedelta(days=1),
                )
                .order_by(Comprovante.created_at)
            )
        ).scalars().all()

        for candidato in candidatos:
            documento_existente = _so_digitos(candidato.cpf_cnpj)
            if documento and documento_existente:
                if documento == documento_existente:
                    return candidato
                continue
            nome_existente = _nome_normalizado(candidato.favorecido)
            if nome and nome_existente and nome == nome_existente:
                return candidato
        return None

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

    async def _get_com_transacao_or_404(
        self, comprovante_id: UUID
    ) -> tuple[Comprovante, Transacao | None]:
        result = await self._db.execute(
            select(Comprovante, Transacao)
            .outerjoin(
                Transacao,
                and_(
                    Comprovante.transacao_id == Transacao.id,
                    Transacao.empresa_id == self._empresa_id,
                ),
            )
            .where(
                Comprovante.id == comprovante_id,
                Comprovante.empresa_id == self._empresa_id,
                Comprovante.deleted_at == None,
            )
        )
        row = result.one_or_none()
        if not row:
            raise NotFoundError(message="Comprovante não encontrado.")
        return row[0], row[1]
