"""Serviço de Notas Fiscais (NF-e / NFS-e).

Responsabilidades:
- CRUD de notas fiscais.
- Associação manual de nota a uma transação bancária.
- Desassociação.
- Filtros por tipo, status, período.
- Import de XML NF-e e NFS-e (individual ou ZIP).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import NotaFiscal, Transacao
from src.domain.notas.xml_parser import NotaParseada, parse_nota_xml
from src.schemas.notas import (
    AssociarTransacaoRequest,
    NotaFiscalCreate,
    NotaFiscalListResponse,
    NotaFiscalResponse,
)


logger = structlog.get_logger(__name__)


@dataclass
class ImportXmlResult:
    importadas: int = 0
    duplicadas: int = 0
    erros: list[str] = field(default_factory=list)


class NotaService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def listar(
        self,
        page: int = 1,
        page_size: int = 50,
        tipo: str | None = None,
        status: str | None = None,
    ) -> NotaFiscalListResponse:
        q = select(NotaFiscal).where(
            NotaFiscal.empresa_id == self._empresa_id,
            NotaFiscal.deleted_at == None,
        )
        if tipo:
            q = q.where(NotaFiscal.tipo == tipo)
        if status:
            q = q.where(NotaFiscal.status == status)

        count_q = select(func.count()).select_from(q.subquery())
        total = (await self._db.execute(count_q)).scalar_one()

        rows = (
            await self._db.execute(
                q.order_by(NotaFiscal.data_emissao.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        return NotaFiscalListResponse(
            items=[NotaFiscalResponse.model_validate(n) for n in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def obter(self, nota_id: UUID) -> NotaFiscalResponse:
        nota = await self._get_or_404(nota_id)
        return NotaFiscalResponse.model_validate(nota)

    async def criar(self, data: NotaFiscalCreate) -> NotaFiscalResponse:
        # Chave de acesso única (se informada)
        if data.chave_acesso:
            existing = await self._db.execute(
                select(NotaFiscal).where(NotaFiscal.chave_acesso == data.chave_acesso)
            )
            if existing.scalar_one_or_none():
                raise ConflictError(
                    message=f"Nota com chave de acesso '{data.chave_acesso}' já importada."
                )

        nota = NotaFiscal(
            empresa_id=self._empresa_id,
            tipo=data.tipo,
            numero=data.numero,
            serie=data.serie,
            cnpj_emitente=data.cnpj_emitente,
            nome_emitente=data.nome_emitente,
            cnpj_destinatario=data.cnpj_destinatario,
            valor=data.valor,
            data_emissao=data.data_emissao,
            chave_acesso=data.chave_acesso,
            observacao=data.observacao,
        )
        self._db.add(nota)
        await self._db.flush()

        logger.info(
            "nota.criada",
            nota_id=str(nota.id),
            tipo=nota.tipo,
            numero=nota.numero,
            empresa_id=str(self._empresa_id),
        )
        return NotaFiscalResponse.model_validate(nota)

    async def associar_transacao(
        self, nota_id: UUID, req: AssociarTransacaoRequest
    ) -> NotaFiscalResponse:
        nota = await self._get_or_404(nota_id)

        if nota.status == "cancelada":
            raise ValidationError(message="Nota cancelada não pode ser associada.")

        if nota.transacao_id:
            raise ConflictError(
                message="Esta nota já está associada a uma transação. Desassocie primeiro."
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

        nota.transacao_id = req.transacao_id
        nota.status = "associada"
        await self._db.flush()

        logger.info(
            "nota.associada",
            nota_id=str(nota_id),
            transacao_id=str(req.transacao_id),
        )
        return NotaFiscalResponse.model_validate(nota)

    async def desassociar_transacao(self, nota_id: UUID) -> NotaFiscalResponse:
        nota = await self._get_or_404(nota_id)

        if nota.status != "associada":
            raise ValidationError(message="Nota não está associada a nenhuma transação.")

        nota.transacao_id = None
        nota.status = "pendente"
        await self._db.flush()

        logger.info("nota.desassociada", nota_id=str(nota_id))
        return NotaFiscalResponse.model_validate(nota)

    async def cancelar(self, nota_id: UUID) -> NotaFiscalResponse:
        nota = await self._get_or_404(nota_id)
        nota.status = "cancelada"
        nota.transacao_id = None
        await self._db.flush()
        logger.info("nota.cancelada", nota_id=str(nota_id))
        return NotaFiscalResponse.model_validate(nota)

    async def importar_xml(self, conteudo: bytes, nome_arquivo: str) -> ImportXmlResult:
        """Faz parse de um XML de NF-e ou NFS-e e persiste a nota."""
        resultado = ImportXmlResult()
        try:
            nota_parseada = parse_nota_xml(conteudo)
        except ValueError as exc:
            resultado.erros.append(f"{nome_arquivo}: {exc}")
            return resultado

        try:
            await self.criar(
                NotaFiscalCreate(
                    tipo=nota_parseada.tipo,
                    numero=nota_parseada.numero,
                    serie=nota_parseada.serie,
                    cnpj_emitente=nota_parseada.cnpj_emitente,
                    nome_emitente=nota_parseada.nome_emitente,
                    cnpj_destinatario=nota_parseada.cnpj_destinatario,
                    valor=nota_parseada.valor,
                    data_emissao=nota_parseada.data_emissao,
                    chave_acesso=nota_parseada.chave_acesso,
                    observacao=nota_parseada.observacao,
                )
            )
            resultado.importadas += 1
        except ConflictError:
            resultado.duplicadas += 1
        except (ValueError, ValidationError) as exc:
            resultado.erros.append(f"{nome_arquivo}: {exc}")

        return resultado

    async def importar_zip(self, conteudo: bytes) -> ImportXmlResult:
        """Descompacta um ZIP e processa cada arquivo .xml dentro dele."""
        resultado = ImportXmlResult()
        try:
            with zipfile.ZipFile(BytesIO(conteudo)) as zf:
                xml_entries = [
                    name for name in zf.namelist()
                    if name.lower().endswith(".xml")
                ]
                if not xml_entries:
                    resultado.erros.append("ZIP não contém nenhum arquivo .xml.")
                    return resultado

                for nome in xml_entries:
                    try:
                        xml_bytes = zf.read(nome)
                    except Exception as exc:
                        resultado.erros.append(f"{nome}: erro ao ler arquivo do ZIP: {exc}")
                        continue

                    parcial = await self.importar_xml(xml_bytes, nome)
                    resultado.importadas += parcial.importadas
                    resultado.duplicadas += parcial.duplicadas
                    resultado.erros.extend(parcial.erros)

        except zipfile.BadZipFile as exc:
            resultado.erros.append(f"Arquivo ZIP inválido: {exc}")

        return resultado

    async def _get_or_404(self, nota_id: UUID) -> NotaFiscal:
        result = await self._db.execute(
            select(NotaFiscal).where(
                NotaFiscal.id == nota_id,
                NotaFiscal.empresa_id == self._empresa_id,
                NotaFiscal.deleted_at == None,
            )
        )
        nota = result.scalar_one_or_none()
        if not nota:
            raise NotFoundError(message="Nota fiscal não encontrada.")
        return nota
