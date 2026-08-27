"""Execução e recuperação dos jobs persistentes.

O trabalho roda no mesmo processo do FastAPI, portanto não pretende substituir
uma fila externa. A persistência serve para acompanhamento, histórico e para
transformar em falha explícita uma execução abandonada por queda do worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Job
from src.db.session import _get_factory
from src.domain.extrato.service import ExtratoService
from src.domain.neo.engine import NeoEngine

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 15
STALE_HEARTBEAT_SECONDS = 120
PROGRESS_BATCH_ITEMS = 25
PROGRESS_BATCH_SECONDS = 2.0

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@asynccontextmanager
async def _production_session_scope():
    async with _get_factory()() as session:
        yield session


@dataclass(frozen=True)
class JobRuntime:
    """Como o background abre e confirma sessões.

    A injeção existe para os testes SQLite poderem executar BackgroundTasks
    inline sobre a sessão isolada do próprio teste. Em produção, cada abertura
    cria uma AsyncSession independente da requisição que já terminou.
    """

    session_scope: SessionScope = _production_session_scope
    commit: bool = True


async def _persistir(db: AsyncSession, runtime: JobRuntime) -> None:
    if runtime.commit:
        await db.commit()
    else:
        await db.flush()


async def _atualizar_job(runtime: JobRuntime, job_id: UUID, **valores: Any) -> None:
    async with runtime.session_scope() as db:
        await db.execute(update(Job).where(Job.id == job_id).values(**valores))
        await _persistir(db, runtime)


async def _heartbeat(runtime: JobRuntime, job_id: UUID) -> None:
    """Mantém o pulso vivo sem depender de avanço no algoritmo principal."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            await _atualizar_job(runtime, job_id, heartbeat_em=datetime.now(UTC))
        except Exception:
            # Um erro transitório não pode encerrar silenciosamente justamente
            # o mecanismo usado para provar que este worker continua vivo.
            logger.exception("job.heartbeat.falhou", job_id=str(job_id))


def _payload(resultado: BaseModel) -> dict[str, Any]:
    return resultado.model_dump(mode="json")


def _erro_para_contador(exc: Exception) -> str:
    mensagem = getattr(exc, "message", None) or str(exc).strip()
    return mensagem or "Não foi possível concluir o processamento. Tente novamente."


async def _iniciar(runtime: JobRuntime, job_id: UUID) -> None:
    agora = datetime.now(UTC)
    await _atualizar_job(runtime, job_id, status="processando", iniciado_em=agora,
                         heartbeat_em=agora, erro=None)


async def _concluir(runtime: JobRuntime, job_id: UUID, resultado: BaseModel, *,
                    com_alertas: bool, total: int) -> None:
    agora = datetime.now(UTC)
    await _atualizar_job(
        runtime, job_id,
        status="concluido_com_alertas" if com_alertas else "concluido",
        total=total, processados=total, resultado=_payload(resultado),
        concluido_em=agora, heartbeat_em=agora,
    )


async def _falhar(runtime: JobRuntime, job_id: UUID, exc: Exception) -> None:
    agora = datetime.now(UTC)
    await _atualizar_job(runtime, job_id, status="falhou", erro=_erro_para_contador(exc),
                         concluido_em=agora, heartbeat_em=agora)


async def executar_neo(job_id: UUID, empresa_id: UUID, agencia_id: UUID | None,
                       mes: str | None, runtime: JobRuntime) -> None:
    """Executa o NEO usando sessão que não pertence à requisição criadora."""
    heartbeat: asyncio.Task | None = None
    try:
        await _iniciar(runtime, job_id)
        heartbeat = asyncio.create_task(_heartbeat(runtime, job_id))
        ultimo_persistido = 0
        ultima_escrita = monotonic()

        async def reportar(processados: int, total: int) -> None:
            nonlocal ultimo_persistido, ultima_escrita
            agora = monotonic()
            deve_escrever = (
                processados == 0 or processados == total
                or processados - ultimo_persistido >= PROGRESS_BATCH_ITEMS
                or agora - ultima_escrita >= PROGRESS_BATCH_SECONDS
            )
            if not deve_escrever:
                return
            await _atualizar_job(runtime, job_id, total=total, processados=processados,
                                 heartbeat_em=datetime.now(UTC))
            ultimo_persistido = processados
            ultima_escrita = agora

        async with runtime.session_scope() as db:
            resultado = await NeoEngine(db=db, empresa_id=empresa_id).processar(
                agencia_id=agencia_id, mes=mes, progresso=reportar
            )
            await _persistir(db, runtime)
        await _concluir(runtime, job_id, resultado, com_alertas=resultado.erros > 0,
                        total=resultado.total_pendentes)
    except Exception as exc:
        logger.exception("job.neo.falhou", job_id=str(job_id), empresa_id=str(empresa_id))
        await _falhar(runtime, job_id, exc)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat


async def executar_importacao_extrato(job_id: UUID, empresa_id: UUID, agencia_id: UUID,
                                      nome_arquivo: str, conteudo_bytes: bytes,
                                      runtime: JobRuntime,
                                      criado_por: UUID | None = None) -> None:
    """Importa OFX/PDF no background, preservando o resultado síncrono no job."""
    heartbeat: asyncio.Task | None = None
    try:
        await _iniciar(runtime, job_id)
        heartbeat = asyncio.create_task(_heartbeat(runtime, job_id))
        async with runtime.session_scope() as db:
            resultado = await _importar_extrato(db, empresa_id, agencia_id,
                                                nome_arquivo, conteudo_bytes,
                                                criado_por)
            await _persistir(db, runtime)
        await _concluir(runtime, job_id, resultado,
                        com_alertas=resultado.rejeitadas > 0,
                        total=resultado.total_no_arquivo)
    except Exception as exc:
        logger.exception("job.extrato.falhou", job_id=str(job_id), empresa_id=str(empresa_id))
        await _falhar(runtime, job_id, exc)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat


async def _importar_extrato(db: AsyncSession, empresa_id: UUID, agencia_id: UUID,
                            nome_arquivo: str, conteudo_bytes: bytes,
                            criado_por: UUID | None = None):
    """Executa o fluxo legado de importação dentro da sessão do job."""
    from src.domain.extrato.importacoes import abrir_importacao, registrar_resultado

    svc = ExtratoService(db=db, empresa_id=empresa_id)
    # O lote nasce ANTES do parse: saber que alguém tentou subir um arquivo que
    # o sistema recusou é informação de suporte, e some se o registro depender
    # da leitura ter dado certo.
    importacao = await abrir_importacao(
        db,
        empresa_id=empresa_id,
        agencia_id=agencia_id,
        nome_arquivo=nome_arquivo,
        conteudo=conteudo_bytes,
        criado_por=criado_por,
    )
    if nome_arquivo.endswith(".pdf"):
        from starlette.concurrency import run_in_threadpool
        from src.core.config import get_settings
        from src.core.errors import ValidationError
        from src.domain.extrato.pdf_parser import PDFParseError, parse_pdf
        # O banco vem da agência cadastrada, não do arquivo: quem escolhe o
        # adaptador de layout é o cadastro, e o PDF só confirma. Agência sem
        # sigla preenchida cai na detecção por conteúdo, dentro do parser.
        agencia = await svc._get_agencia_or_400(agencia_id)
        try:
            timeout = get_settings().pdf_parse_timeout_seconds + 5
            transacoes = await asyncio.wait_for(
                run_in_threadpool(parse_pdf, conteudo_bytes, agencia.banco_sigla),
                timeout=timeout,
            )
        except TimeoutError:
            raise ValidationError(message="Processamento do PDF excedeu o tempo limite.") from None
        except PDFParseError as exc:
            raise ValidationError(message=f"Arquivo PDF inválido: {exc}") from exc
        resultado = await svc.importar_transacoes_raw(
            transacoes, agencia_id, importacao_id=importacao.id
        )
        await registrar_resultado(db, importacao, resultado)
        return resultado

    try:
        conteudo = conteudo_bytes.decode("utf-8")
    except UnicodeDecodeError:
        conteudo = conteudo_bytes.decode("latin-1")
    resultado = await svc.importar_ofx(conteudo, agencia_id, importacao_id=importacao.id)
    await registrar_resultado(db, importacao, resultado)
    return resultado


async def recuperar_jobs_sem_heartbeat(runtime: JobRuntime | None = None) -> int:
    """Falha somente execuções cujo heartbeat parou há mais de dois minutos.

    Cada worker chama esta função no startup. O predicado por heartbeat torna
    isso seguro com N workers: um startup parcial não mata o job saudável do
    vizinho, e chamadas concorrentes só encontram linhas ainda elegíveis.
    Reduzir isto para "falhar todos os processando" reintroduz essa corrida.

    ``na_fila`` também entra no predicado: depois de dois minutos sem pulso a
    BackgroundTask já se perdeu e não há fila externa capaz de retomá-la. O
    heartbeat nasce junto com a linha para cobrir a janela entre commit e início.
    """
    runtime = runtime or JobRuntime()
    limite = datetime.now(UTC) - timedelta(seconds=STALE_HEARTBEAT_SECONDS)
    agora = datetime.now(UTC)
    async with runtime.session_scope() as db:
        resultado = await db.execute(
            update(Job).where(
                Job.status.in_(("na_fila", "processando")),
                Job.heartbeat_em < limite,
            ).values(
                status="falhou",
                erro="Processamento interrompido porque o worker parou de enviar heartbeat. Inicie uma nova execução.",
                concluido_em=agora,
            )
        )
        await _persistir(db, runtime)
        return resultado.rowcount or 0
