"""Serviço de Notas Fiscais (NF-e / NFS-e).

Responsabilidades:
- CRUD de notas fiscais.
- Associação manual de nota a uma transação bancária.
- Desassociação.
- Filtros por tipo, status, período.
- Import de XML NF-e e NFS-e (individual ou ZIP).
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import Empresa, NotaFiscal, Transacao
from src.domain.notas.xml_parser import parse_nota_xml
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
        chave_acesso = _normalizar_chave_acesso(data.chave_acesso)
        dedup_key = _calcular_identidade_nota(data, chave_acesso)
        existing = await self._db.execute(
            select(NotaFiscal).where(
                NotaFiscal.empresa_id == self._empresa_id,
                NotaFiscal.dedup_key == dedup_key,
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError(message="Esta nota fiscal já foi cadastrada nesta empresa.")

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
            chave_acesso=chave_acesso,
            dedup_key=dedup_key,
            observacao=data.observacao,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(nota)
                await self._db.flush()
        except IntegrityError as exc:
            raise ConflictError(
                message="Esta nota fiscal já foi cadastrada nesta empresa."
            ) from exc

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

        empresa = (
            await self._db.execute(
                select(Empresa).where(Empresa.id == self._empresa_id)
            )
        ).scalar_one_or_none()
        if empresa is None:
            resultado.erros.append(f"{nome_arquivo}: empresa do contexto não encontrada.")
            return resultado
        cnpj_empresa = _somente_digitos(empresa.cnpj)
        envolvidos = {
            _somente_digitos(nota_parseada.cnpj_emitente),
            _somente_digitos(nota_parseada.cnpj_destinatario),
        }
        if cnpj_empresa not in envolvidos:
            resultado.erros.append(
                f"{nome_arquivo}: CNPJ da empresa não consta como emitente nem destinatário."
            )
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
                infos = zf.infolist()
                erro_limite = _validar_zip(infos)
                if erro_limite:
                    resultado.erros.append(erro_limite)
                    return resultado
                xml_entries = [info for info in infos if info.filename.lower().endswith(".xml")]
                if not xml_entries:
                    resultado.erros.append("ZIP não contém nenhum arquivo .xml.")
                    return resultado

                for info in xml_entries:
                    nome = info.filename
                    try:
                        with zf.open(info) as entry:
                            xml_bytes = entry.read(_MAX_XML_BYTES + 1)
                        if len(xml_bytes) > _MAX_XML_BYTES:
                            resultado.erros.append(f"{nome}: XML excede o limite de 10 MiB.")
                            continue
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


_MAX_ZIP_ENTRADAS = 1_000
_MAX_ZIP_DESCOMPACTADO = 100 * 1024 * 1024
_MAX_ZIP_RAZAO_COMPRESSAO = 100
_MAX_XML_BYTES = 10 * 1024 * 1024


def _validar_zip(infos: list[zipfile.ZipInfo]) -> str | None:
    if len(infos) > _MAX_ZIP_ENTRADAS:
        return f"ZIP excede o limite de {_MAX_ZIP_ENTRADAS} entradas."
    if sum(info.file_size for info in infos) > _MAX_ZIP_DESCOMPACTADO:
        return "ZIP excede o limite total descompactado de 100 MiB."
    for info in infos:
        if info.file_size > _MAX_XML_BYTES and info.filename.lower().endswith(".xml"):
            return f"{info.filename}: XML excede o limite de 10 MiB."
        if info.file_size and (
            info.compress_size == 0
            or info.file_size / info.compress_size > _MAX_ZIP_RAZAO_COMPRESSAO
        ):
            return f"{info.filename}: razão de compressão insegura."
    return None


def _somente_digitos(valor: str | None) -> str:
    return re.sub(r"\D", "", valor or "")


def _normalizar_chave_acesso(chave: str | None) -> str | None:
    if chave is None:
        return None
    digits = _somente_digitos(chave)
    if not re.fullmatch(r"\d{44}", digits):
        raise ValidationError(message="Chave de acesso deve conter exatamente 44 dígitos.")
    return digits


def _calcular_identidade_nota(data: NotaFiscalCreate, chave_acesso: str | None) -> str:
    if data.tipo == "nfe":
        if not chave_acesso:
            # Cadastro manual legado sem chave: ainda precisa ser idempotente.
            origem = "|".join((
                "nfe-sem-chave",
                _somente_digitos(data.cnpj_emitente),
                data.numero.strip(),
                (data.serie or "").strip(),
                str(Decimal(str(data.valor)).quantize(Decimal("0.01"))),
                _data_utc(data.data_emissao).isoformat(),
            ))
        else:
            origem = f"nfe|{chave_acesso}"
    else:
        origem = "|".join((
            "nfse",
            _somente_digitos(data.cnpj_emitente),
            data.numero.strip(),
            (data.serie or "").strip(),
            str(Decimal(str(data.valor)).quantize(Decimal("0.01"))),
            _data_utc(data.data_emissao).isoformat(),
        ))
    return hashlib.sha256(origem.encode("utf-8")).hexdigest()


def _data_utc(data: datetime) -> datetime:
    if data.tzinfo is None:
        return data.replace(tzinfo=UTC)
    return data.astimezone(UTC)
