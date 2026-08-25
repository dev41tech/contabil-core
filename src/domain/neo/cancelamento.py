"""Cancelamento de um lançamento contábil gerado pelo NEO.

Desfaz a classificação de uma transação: apaga o par de partidas, devolve a
transação para a fila e solta os documentos que tinham sido vinculados.

POR QUE "CANCELAR" E NÃO "ESTORNAR"

São dois verbos diferentes, e a escolha depende de o período já ter saído do
sistema. Estorno é aditivo — mantém o lançamento original e acrescenta o par
inverso —, e é o que a prática contábil pede quando o período já virou balancete
ou entrega. Cancelamento apaga o par porque ele nunca valeu para ninguém.

Aqui é cancelamento porque **não existe fechamento de período no sistema**. Sem
essa noção, não há como distinguir o lançamento que já saiu daquele que nunca
saiu, e apagar é a operação correta e mais simples. Quando houver fechamento, o
estorno entra ao lado desta função, não no lugar dela.

Há ainda uma razão técnica: `uq_registro_transacao_dc_ativo` garante no máximo
um débito e um crédito ATIVOS por transação. Um estorno aditivo colidiria com
esse índice, que existe para impedir dupla contabilização e deve continuar
existindo. Cancelar satisfaz o índice sem mudar nada no schema.

A UNIDADE É O LANÇAMENTO

A operação recebe `lancamento_id`, nunca `registro_id`. Um par de partidas é
balanceado por construção, e aceitar uma linha só permitiria desfazer metade e
deixar o razão torto. A assinatura é onde esse erro se previne.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError
from src.db.models import (
    Comprovante,
    NeoDecisao,
    NotaFiscal,
    RegistroContabil,
    Transacao,
)
from src.domain.auditoria.service import registrar_auditoria

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CancelamentoResultado:
    transacao_id: UUID
    partidas_canceladas: int
    notas_desvinculadas: int
    comprovantes_desvinculados: int


async def cancelar_lancamento(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    lancamento_id: UUID,
    motivo: str,
    usuario_id: UUID | None = None,
) -> CancelamentoResultado:
    """Desfaz um lançamento e devolve a transação para a fila de classificação.

    Tudo acontece na transação de banco de quem chama: partidas, status,
    decisão, desvínculos e auditoria entram ou não entram juntos. Metade
    aplicada é pior que nada aplicado — deixaria uma transação pendente com
    partidas vivas, que é dupla contabilização esperando acontecer.
    """
    partidas = (
        (
            await db.execute(
                select(RegistroContabil).where(
                    RegistroContabil.lancamento_id == lancamento_id,
                    RegistroContabil.empresa_id == empresa_id,
                    RegistroContabil.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not partidas:
        # Vale para lançamento inexistente e para lançamento já cancelado. A
        # distinção não interessa a quem chama: nos dois casos não há o que
        # desfazer, e insistir devolve o mesmo.
        raise NotFoundError(
            message="Lançamento não encontrado ou já cancelado."
        )

    transacao_id = partidas[0].transacao_id
    if transacao_id is None:
        raise ConflictError(
            message=(
                "Este lançamento não veio de uma transação do extrato e não "
                "pode ser cancelado por aqui."
            )
        )

    # Trava ANTES de ler o status: checar e só depois travar é a corrida que
    # deixa dois cancelamentos simultâneos passarem pela mesma porta.
    transacao = (
        await db.execute(
            select(Transacao)
            .where(
                Transacao.id == transacao_id,
                Transacao.empresa_id == empresa_id,
                Transacao.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if transacao is None:
        raise NotFoundError(message="Transação do lançamento não encontrada.")

    agora = datetime.now(UTC)
    dados_antes = {
        "transacao_id": str(transacao_id),
        "status_transacao": transacao.status,
        "partidas": [
            {
                "conta_id": str(p.conta_id),
                "descricao": p.descricao,
                "dc": p.dc,
                "valor": str(p.valor),
                "tipo_regra": p.tipo_regra,
            }
            for p in partidas
        ],
    }

    for partida in partidas:
        partida.deleted_at = agora
        partida.cancelado_em = agora
        partida.cancelado_por = usuario_id
        partida.motivo_cancelamento = motivo[:300]

    transacao.status = "pendente"
    # Marca a recusa: a transação volta para a fila, mas para decisão HUMANA.
    # Sem isto, a mesma regra que classificou reclassificaria na proxima
    # execução do motor, e desfazer não teria efeito nenhum.
    transacao.auto_recusado_em = agora
    transacao.auto_recusado_por = usuario_id

    # Documentos são DESVINCULADOS, nunca apagados: o documento não tem nada de
    # errado e precisa ficar livre para a próxima classificação reencontrá-lo.
    notas = (
        (
            await db.execute(
                select(NotaFiscal).where(
                    NotaFiscal.transacao_id == transacao_id,
                    NotaFiscal.empresa_id == empresa_id,
                    NotaFiscal.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for nota in notas:
        nota.transacao_id = None
        nota.status = "pendente"

    comprovantes = (
        (
            await db.execute(
                select(Comprovante).where(
                    Comprovante.transacao_id == transacao_id,
                    Comprovante.empresa_id == empresa_id,
                    Comprovante.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for comprovante in comprovantes:
        comprovante.transacao_id = None

    # `NeoDecisao` é log: o cancelamento vira uma linha NOVA. Editar a decisão
    # anterior apagaria o registro de que o motor um dia classificou aquilo.
    db.add(
        NeoDecisao(
            empresa_id=empresa_id,
            transacao_id=transacao_id,
            resultado="sem_regra",
            estrategia="manual",
            motivo=f"Classificação cancelada: {motivo}"[:500],
            processado_em=agora,
        )
    )

    await registrar_auditoria(
        db,
        acao="lancamento.cancelado",
        entidade="lancamento",
        entidade_id=lancamento_id,
        dados_antes=dados_antes,
        dados_depois={
            "motivo": motivo,
            "status_transacao": "pendente",
            "notas_desvinculadas": len(notas),
            "comprovantes_desvinculados": len(comprovantes),
        },
        empresa_id=empresa_id,
        usuario_id=usuario_id,
    )

    logger.info(
        "neo.lancamento_cancelado",
        lancamento_id=str(lancamento_id),
        transacao_id=str(transacao_id),
        partidas=len(partidas),
    )
    return CancelamentoResultado(
        transacao_id=transacao_id,
        partidas_canceladas=len(partidas),
        notas_desvinculadas=len(notas),
        comprovantes_desvinculados=len(comprovantes),
    )


async def liberar_para_automatico(
    db: AsyncSession,
    *,
    empresa_id: UUID,
    transacao_id: UUID,
    usuario_id: UUID | None = None,
) -> None:
    """Devolve a transação ao motor, desfazendo a recusa da classificação automática.

    Existe porque desfazer por engano, ou para testar, não pode condenar a
    transação a esperar decisão manual para sempre. Sem esta porta, a única
    saída seria classificar à mão algo que a regra já classificava bem.

    Não mexe em regra nenhuma: só remove o bloqueio desta transação.
    """
    transacao = (
        await db.execute(
            select(Transacao)
            .where(
                Transacao.id == transacao_id,
                Transacao.empresa_id == empresa_id,
                Transacao.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if transacao is None:
        raise NotFoundError(message="Transação não encontrada.")
    if transacao.auto_recusado_em is None:
        raise ConflictError(
            message="Esta transação já está liberada para classificação automática."
        )

    antes = transacao.auto_recusado_em
    transacao.auto_recusado_em = None
    transacao.auto_recusado_por = None

    await registrar_auditoria(
        db,
        acao="transacao.liberada_para_automatico",
        entidade="transacao",
        entidade_id=transacao_id,
        dados_antes={"auto_recusado_em": str(antes)},
        dados_depois={"auto_recusado_em": None},
        empresa_id=empresa_id,
        usuario_id=usuario_id,
    )
    logger.info(
        "neo.transacao_liberada_para_automatico", transacao_id=str(transacao_id)
    )
