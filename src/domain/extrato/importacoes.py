"""Lotes de importação de extrato — criar, listar e cancelar.

Cancelar um lote é a operação que faltava para "subi o arquivo errado". Ela se
apoia no cancelamento de lançamento (fase 01): transação já contabilizada não
pode simplesmente sumir, porque as partidas dela ficariam órfãs no razão.

A ORDEM IMPORTA

Para cada transação do lote:

1. se tem lançamento vigente, cancela o lançamento primeiro — isso já devolve a
   transação para `pendente`, solta notas e comprovantes e escreve a auditoria;
2. só então a transação recebe `deleted_at`.

Fazer o inverso deixaria, no intervalo entre os dois passos, uma transação
apagada com partidas vivas — que é exatamente o estado que ninguém consegue
enxergar nem corrigir pela tela.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError
from src.db.models import ExtratoImportacao, RegistroContabil, Transacao
from src.domain.auditoria.service import registrar_auditoria
from src.domain.neo.cancelamento import cancelar_lancamento

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CancelamentoLoteResultado:
    importacao_id: UUID
    transacoes_removidas: int
    lancamentos_cancelados: int


def hash_do_arquivo(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


async def abrir_importacao(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    agencia_id: UUID,
    nome_arquivo: str,
    conteudo: bytes,
    criado_por: UUID | None,
) -> ExtratoImportacao:
    """Registra o lote ANTES de parsear, para o arquivo ficar rastreado mesmo
    quando a leitura falha — saber que alguém tentou subir um arquivo que o
    sistema recusou é informação de suporte."""
    importacao = ExtratoImportacao(
        empresa_id=empresa_id,
        agencia_id=agencia_id,
        nome_arquivo=(nome_arquivo or "sem-nome")[:255],
        hash_arquivo=hash_do_arquivo(conteudo),
        criado_por=criado_por,
    )
    db.add(importacao)
    await db.flush()
    return importacao


async def ancora_de_saldo(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    agencia_id: UUID,
    antes_de: date,
) -> ExtratoImportacao | None:
    """Último lote desta conta com fechamento declarado ANTES de ``antes_de``.

    Ordena por `data_saldo_declarado`, não por `created_at`: é o que faz a
    conferência sobreviver a upload fora de ordem. Subir junho e depois
    fevereiro não elege junho como âncora de fevereiro — a âncora é o período
    anterior, independentemente de quando o arquivo foi enviado.

    A comparação é estritamente menor de propósito. Reenviar o MESMO arquivo
    encontraria a si próprio como âncora e acusaria diferença igual ao movimento
    inteiro do período; com `<`, o reenvio simplesmente não tem âncora e nada é
    alegado.

    Lote cancelado não serve de âncora: as transações dele foram removidas, e o
    fechamento que ele declara não corresponde mais a nada no banco.
    """
    return (
        await db.execute(
            select(ExtratoImportacao)
            .where(
                ExtratoImportacao.empresa_id == empresa_id,
                ExtratoImportacao.agencia_id == agencia_id,
                ExtratoImportacao.deleted_at.is_(None),
                ExtratoImportacao.cancelada_em.is_(None),
                ExtratoImportacao.data_saldo_declarado.is_not(None),
                ExtratoImportacao.data_saldo_declarado < antes_de,
            )
            .order_by(ExtratoImportacao.data_saldo_declarado.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def registrar_resultado(
    db: AsyncSession, importacao: ExtratoImportacao, resultado
) -> None:
    """Guarda no lote o que a importação produziu."""
    importacao.total_no_arquivo = getattr(resultado, "total_no_arquivo", 0) or 0
    importacao.importadas = getattr(resultado, "importadas", 0) or 0
    importacao.duplicadas = getattr(resultado, "duplicadas", 0) or 0
    importacao.rejeitadas = getattr(resultado, "rejeitadas", 0) or 0
    # Só o OFX declara fechamento de período; no PDF estes três ficam nulos, e a
    # completude de lá continua sendo a cadeia de saldos por lançamento.
    importacao.saldo_declarado = getattr(resultado, "saldo_declarado", None)
    importacao.data_saldo_declarado = getattr(resultado, "data_saldo_declarado", None)
    importacao.alerta_saldo = getattr(resultado, "alerta_saldo", None)


async def cancelar_importacao(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    importacao_id: UUID,
    motivo: str,
    usuario_id: UUID | None = None,
) -> CancelamentoLoteResultado:
    """Desfaz um upload inteiro: partidas, classificações e transações."""
    importacao = (
        await db.execute(
            select(ExtratoImportacao)
            .where(
                ExtratoImportacao.id == importacao_id,
                ExtratoImportacao.empresa_id == empresa_id,
                ExtratoImportacao.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if importacao is None:
        raise NotFoundError(message="Importação não encontrada.")
    if importacao.cancelada_em is not None:
        raise ConflictError(message="Esta importação já foi cancelada.")

    transacoes = (
        (
            await db.execute(
                select(Transacao)
                .where(
                    Transacao.importacao_id == importacao_id,
                    Transacao.empresa_id == empresa_id,
                    Transacao.deleted_at.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )

    lancamentos_cancelados = 0
    agora = datetime.now(UTC)
    for transacao in transacoes:
        lancamento_id = (
            await db.execute(
                select(RegistroContabil.lancamento_id).where(
                    RegistroContabil.transacao_id == transacao.id,
                    RegistroContabil.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if lancamento_id is not None:
            # Primeiro o lançamento: apagar a transação antes deixaria partidas
            # órfãs no razão, sem transação para explicá-las.
            await cancelar_lancamento(
                db,
                empresa_id=empresa_id,
                lancamento_id=lancamento_id,
                motivo=f"Importação cancelada: {motivo}",
                usuario_id=usuario_id,
            )
            lancamentos_cancelados += 1
        transacao.deleted_at = agora

    importacao.cancelada_em = agora
    importacao.cancelada_por = usuario_id
    importacao.motivo_cancelamento = motivo[:300]

    await registrar_auditoria(
        db,
        acao="extrato_importacao.cancelada",
        entidade="extrato_importacao",
        entidade_id=importacao_id,
        dados_antes={
            "nome_arquivo": importacao.nome_arquivo,
            "importadas": importacao.importadas,
            "transacoes_ativas": len(transacoes),
        },
        dados_depois={
            "motivo": motivo,
            "transacoes_removidas": len(transacoes),
            "lancamentos_cancelados": lancamentos_cancelados,
        },
        empresa_id=empresa_id,
        usuario_id=usuario_id,
    )

    logger.info(
        "extrato.importacao_cancelada",
        importacao_id=str(importacao_id),
        transacoes=len(transacoes),
        lancamentos=lancamentos_cancelados,
    )
    return CancelamentoLoteResultado(
        importacao_id=importacao_id,
        transacoes_removidas=len(transacoes),
        lancamentos_cancelados=lancamentos_cancelados,
    )


async def listar_importacoes(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    agencia_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ExtratoImportacao], list[int], int]:
    """Lotes da empresa, do mais recente para o mais antigo.

    Devolve também quantas transações de cada lote ainda estão ativas — é o que
    diz se ainda há o que cancelar, e o número diverge de `importadas` assim que
    alguma transação é removida individualmente.
    """
    q = select(ExtratoImportacao).where(
        ExtratoImportacao.empresa_id == empresa_id,
        ExtratoImportacao.deleted_at.is_(None),
    )
    if agencia_id:
        q = q.where(ExtratoImportacao.agencia_id == agencia_id)

    total = (
        await db.execute(select(func.count()).select_from(q.subquery()))
    ).scalar_one()

    itens = (
        (
            await db.execute(
                q.order_by(ExtratoImportacao.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    ativas_por_lote: dict[UUID, int] = {}
    if itens:
        linhas = (
            await db.execute(
                select(Transacao.importacao_id, func.count())
                .where(
                    Transacao.importacao_id.in_([i.id for i in itens]),
                    Transacao.deleted_at.is_(None),
                )
                .group_by(Transacao.importacao_id)
            )
        ).all()
        ativas_por_lote = dict(linhas)

    return list(itens), [ativas_por_lote.get(i.id, 0) for i in itens], total
