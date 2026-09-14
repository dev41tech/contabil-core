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
from src.db.models import AgenciaBancaria, ExtratoImportacao, RegistroContabil, Transacao
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


CODIGO_EXTRATO_JA_IMPORTADO = "EXTRATO_JA_IMPORTADO"


async def lote_com_o_mesmo_arquivo(
    db: AsyncSession, *, empresa_id: UUID, hash_arquivo: str
) -> tuple[ExtratoImportacao, int, AgenciaBancaria | None] | None:
    """Lote vivo desta empresa com exatamente este arquivo, e o que ele ainda tem.

    O HASH ERA GRAVADO E NUNCA LIDO

    `abrir_importacao` guarda `hash_arquivo` em todo lote desde que os lotes
    existem, e nada o consultava. Reenviar o mesmo extrato abria um lote novo; a
    deduplicação por linha impedia as transações de duplicar, então o lote nascia
    com 0 importadas e selo de "Concluída" — e, sem transação, o "Desfazer" ficava
    desabilitado. O card repetido não saía mais da tela.

    O QUE CONTA COMO "O ARQUIVO JÁ ESTÁ NO SISTEMA"

    Ter transação viva, e não ter sido importado um dia. Três casos em que o
    reenvio TEM de passar:

    - lote desfeito — é o fluxo de conserto: desfaz o errado, sobe de novo;
    - lote que falhou na leitura — o layout pode ter passado a ser lido depois;
    - lote cujas transações foram todas removidas uma a uma — no banco, o
      arquivo já não existe.

    A busca é por empresa, não por conta: o mesmo arquivo enviado para outra
    conta é quase sempre a conta errada, e a mensagem nomeia a de destino.
    """
    vivas = (
        select(func.count())
        .where(
            Transacao.importacao_id == ExtratoImportacao.id,
            Transacao.deleted_at.is_(None),
        )
        .scalar_subquery()
    )
    linha = (
        await db.execute(
            select(ExtratoImportacao, vivas, AgenciaBancaria)
            .outerjoin(AgenciaBancaria, AgenciaBancaria.id == ExtratoImportacao.agencia_id)
            .where(
                ExtratoImportacao.empresa_id == empresa_id,
                ExtratoImportacao.hash_arquivo == hash_arquivo,
                ExtratoImportacao.deleted_at.is_(None),
                ExtratoImportacao.cancelada_em.is_(None),
                vivas > 0,
            )
            .order_by(ExtratoImportacao.created_at)
            .limit(1)
        )
    ).first()
    if linha is None:
        return None
    return linha[0], linha[1], linha[2]


def erro_extrato_ja_importado(
    lote: ExtratoImportacao, transacoes_vivas: int, agencia: AgenciaBancaria | None
) -> ConflictError:
    conta = ""
    if agencia is not None:
        conta = f" na conta {agencia.banco_sigla or ''} ag. {agencia.agencia} c/c {agencia.numero}"
    return ConflictError(
        message=(
            f"Este arquivo já foi importado em {lote.created_at.strftime('%d/%m/%Y')}"
            f"{conta} ({lote.nome_arquivo}, {transacoes_vivas} transações no sistema). "
            "Para importá-lo de novo, desfaça aquela importação na tela de Importações."
        ),
        code=CODIGO_EXTRATO_JA_IMPORTADO,
        details={
            "importacao_id": str(lote.id),
            "agencia_id": str(lote.agencia_id),
            "nome_arquivo": lote.nome_arquivo,
            "importado_em": lote.created_at.isoformat(),
            "transacoes_ativas": transacoes_vivas,
        },
    )


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

    agora = datetime.now(UTC)
    lancamentos_cancelados = await _remover_transacoes(
        db,
        empresa_id=empresa_id,
        transacoes=transacoes,
        motivo_lancamento=f"Importação cancelada: {motivo}",
        usuario_id=usuario_id,
        agora=agora,
    )

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


async def _remover_transacoes(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    transacoes: list[Transacao],
    motivo_lancamento: str,
    usuario_id: UUID | None,
    agora: datetime,
) -> int:
    """Remove transações já travadas, cancelando antes o lançamento de cada uma.

    Devolve quantos lançamentos foram cancelados. Compartilhado entre o lote e o
    conjunto sem lote: a ordem é a mesma nos dois, e é ela que importa.
    """
    lancamentos_cancelados = 0
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
                motivo=motivo_lancamento,
                usuario_id=usuario_id,
            )
            lancamentos_cancelados += 1
        transacao.deleted_at = agora
    return lancamentos_cancelados


# ── Transações sem lote ──────────────────────────────────────────────────────
#
# APAGAR TODAS AS IMPORTAÇÕES NÃO LIMPAVA A EMPRESA
#
# Relato de 2026-09-14 na SINCOPEÇAS: depois de desfazer todas as importações de
# extrato, o Extrato e os Registros ainda mostravam lançamentos.
#
# Os lotes só existem desde a migration 0028 (25/08/2026), que deixou as
# transações anteriores com `importacao_id` nulo — sem backfill de propósito,
# porque nada no banco diz qual upload trouxe qual linha, e inventar lote seria
# pior que não ter. O Open Banking também cria transação sem lote. Nenhuma delas
# aparecia em Importações, e nenhum "Desfazer" as alcançava.
#
# O agrupamento é por CONTA, não por arquivo: é o único conjunto que existe de
# fato no banco. Não finge ser o lote que nunca existiu.


def _sem_lote(empresa_id: UUID):
    return (
        Transacao.empresa_id == empresa_id,
        Transacao.importacao_id.is_(None),
        Transacao.deleted_at.is_(None),
    )


async def transacoes_sem_lote(db: AsyncSession, *, empresa_id: UUID) -> list[dict]:
    """Por conta bancária: quantas transações vivas sem lote, e de que período."""
    lancamentos = (
        select(func.count(func.distinct(RegistroContabil.lancamento_id)))
        .join(Transacao, Transacao.id == RegistroContabil.transacao_id)
        .where(
            *_sem_lote(empresa_id),
            RegistroContabil.deleted_at.is_(None),
            Transacao.agencia_id == AgenciaBancaria.id,
        )
        .scalar_subquery()
    )
    linhas = (
        await db.execute(
            select(
                AgenciaBancaria,
                func.count(Transacao.id),
                func.min(Transacao.data),
                func.max(Transacao.data),
                lancamentos,
            )
            .join(Transacao, Transacao.agencia_id == AgenciaBancaria.id)
            .where(*_sem_lote(empresa_id), AgenciaBancaria.empresa_id == empresa_id)
            .group_by(AgenciaBancaria.id)
            .order_by(AgenciaBancaria.banco_sigla, AgenciaBancaria.agencia, AgenciaBancaria.numero)
        )
    ).all()
    return [
        {
            "agencia_id": agencia.id,
            "banco_sigla": agencia.banco_sigla,
            "agencia": agencia.agencia,
            "numero": agencia.numero,
            "agencia_ativa": agencia.ativa,
            "transacoes_ativas": quantidade,
            "lancamentos_ativos": lancamentos_ativos or 0,
            "primeira_data": primeira,
            "ultima_data": ultima,
        }
        for agencia, quantidade, primeira, ultima, lancamentos_ativos in linhas
    ]


async def remover_transacoes_sem_lote(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    agencia_id: UUID,
    motivo: str,
    usuario_id: UUID | None = None,
) -> tuple[int, int]:
    """Remove as transações sem lote de UMA conta, com os lançamentos delas.

    Mesma operação do "Desfazer" do lote — lançamento cancelado antes, transação
    removida depois, motivo obrigatório e auditoria —, sobre o conjunto que o
    lote não alcança. Por conta, e não da empresa inteira de uma vez: o raio de
    uma ação irreversível pela tela tem de caber no que o contador está vendo.
    """
    agencia = (
        await db.execute(
            select(AgenciaBancaria).where(
                AgenciaBancaria.id == agencia_id,
                AgenciaBancaria.empresa_id == empresa_id,
                AgenciaBancaria.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if agencia is None:
        raise NotFoundError(message="Conta bancária não encontrada.")

    transacoes = list(
        (
            await db.execute(
                select(Transacao)
                .where(*_sem_lote(empresa_id), Transacao.agencia_id == agencia_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not transacoes:
        raise ConflictError(message="Esta conta não tem transações sem lote de importação.")

    lancamentos_cancelados = await _remover_transacoes(
        db,
        empresa_id=empresa_id,
        transacoes=transacoes,
        motivo_lancamento=f"Transações sem lote removidas: {motivo}",
        usuario_id=usuario_id,
        agora=datetime.now(UTC),
    )

    await registrar_auditoria(
        db,
        acao="extrato_sem_lote.removido",
        entidade="agencia_bancaria",
        entidade_id=agencia_id,
        dados_antes={
            "transacoes_ativas": len(transacoes),
            "primeira_data": min(t.data for t in transacoes).isoformat(),
            "ultima_data": max(t.data for t in transacoes).isoformat(),
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
        "extrato.sem_lote_removido",
        agencia_id=str(agencia_id),
        transacoes=len(transacoes),
        lancamentos=lancamentos_cancelados,
    )
    return len(transacoes), lancamentos_cancelados


async def excluir_importacao(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    importacao_id: UUID,
    usuario_id: UUID | None = None,
) -> None:
    """Tira da lista um lote que não tem mais nada no sistema.

    É o caminho que faltava para os cards que o "Desfazer" não alcança: reenvio
    que entrou zerado, arquivo que falhou na leitura, lote já desfeito. Todos têm
    0 transações vivas — e o botão de desfazer ficava desabilitado justamente aí.

    Lote com transação viva é recusado: excluir o registro deixaria transações
    apontando para um lote invisível, sem o "Desfazer" que as removeria junto com
    os lançamentos. Para esse, o caminho é desfazer primeiro.

    Exclusão lógica, com auditoria: o que o lote trouxe e se ele foi desfeito
    continua rastreável.
    """
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

    vivas = (
        await db.execute(
            select(func.count()).where(
                Transacao.importacao_id == importacao_id,
                Transacao.empresa_id == empresa_id,
                Transacao.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    if vivas:
        raise ConflictError(
            message=(
                f"Esta importação ainda tem {vivas} transações no sistema. "
                "Desfaça a importação antes de excluir o registro."
            ),
            code="IMPORTACAO_COM_TRANSACOES",
            details={"transacoes_ativas": vivas},
        )

    importacao.deleted_at = datetime.now(UTC)
    await registrar_auditoria(
        db,
        acao="extrato_importacao.excluida",
        entidade="extrato_importacao",
        entidade_id=importacao_id,
        dados_antes={
            "nome_arquivo": importacao.nome_arquivo,
            "importadas": importacao.importadas,
            "duplicadas": importacao.duplicadas,
            "rejeitadas": importacao.rejeitadas,
            "cancelada_em": (
                importacao.cancelada_em.isoformat() if importacao.cancelada_em else None
            ),
        },
        dados_depois={"excluida": True},
        empresa_id=empresa_id,
        usuario_id=usuario_id,
    )
    logger.info("extrato.importacao_excluida", importacao_id=str(importacao_id))


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
