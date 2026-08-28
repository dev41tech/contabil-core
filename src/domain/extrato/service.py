"""Serviço de Extrato Bancário.

Responsabilidades:
- Importar arquivo OFX e persistir as transações.
- Deduplicação de OFX por SHA-256 de (empresa_id + agencia_id + FITID).
- Listar transações com filtros (status, agência, data).
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError, ValidationError
from src.db.models import AgenciaBancaria, Transacao
from src.domain.extrato.ofx_parser import OFXParseError, TransacaoOFX, parse_ofx_detalhado
from src.domain.extrato.ordenacao import ordenar_como_o_extrato
from src.domain.extrato.validacao import (
    historico_parece_linha_crua,
    motivo_valor_nao_confiavel,
)
from src.schemas.extrato import (
    ExtratoPendentesResponse,
    ImportacaoResult,
    TransacaoFiltro,
    TransacaoResponse,
)

logger = structlog.get_logger(__name__)


class ExtratoService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def importar_ofx(
        self,
        conteudo: str,
        agencia_id: UUID,
        importacao_id: UUID | None = None,
    ) -> ImportacaoResult:
        """Parseia OFX, deduplica e persiste as transações novas."""
        await self._get_agencia_or_400(agencia_id)

        try:
            parse_result = parse_ofx_detalhado(conteudo)
        except OFXParseError as e:
            raise ValidationError(message=f"Arquivo OFX inválido: {e}")

        if parse_result.total_blocos == 0:
            raise ValidationError(message="Nenhuma transação encontrada no arquivo OFX.")

        transacoes_ofx = parse_result.transacoes
        importadas, duplicadas, erros = 0, 0, len(parse_result.erros)
        rejeitadas, motivos_rejeicao = 0, []
        novas: list[Transacao] = []
        hashes_do_lote: set[str] = set()

        for erro in parse_result.erros:
            logger.warning("extrato.ofx.transacao_rejeitada", erro=erro)

        for t in transacoes_ofx:
            try:
                motivo = motivo_valor_nao_confiavel(t.historico, t.valor)
                if motivo:
                    # Não persiste: valor errado entra em silêncio e contamina o
                    # razão, enquanto a linha faltando aparece na conciliação.
                    rejeitadas += 1
                    motivos_rejeicao.append(f"{t.historico[:80]} — {motivo}")
                    logger.warning(
                        "extrato.transacao_rejeitada_valor_suspeito",
                        historico=t.historico[:200],
                        valor=str(t.valor),
                        motivo=motivo,
                    )
                    continue
                if historico_parece_linha_crua(t.historico):
                    logger.warning(
                        "extrato.historico_linha_crua",
                        historico=t.historico[:200],
                    )

                dc = "C" if t.valor >= 0 else "D"
                hash_dedup = _calcular_hash(self._empresa_id, agencia_id, t)

                if hash_dedup in hashes_do_lote:
                    duplicadas += 1
                    continue

                # Só linha VIVA duplica. Linha cancelada mantém o hash no
                # banco (soft delete) e, sem este filtro, bloqueava para
                # sempre a reimportação do arquivo que o próprio usuário
                # cancelou — ver migration 0033.
                existing = await self._db.execute(
                    select(Transacao).where(
                        Transacao.empresa_id == self._empresa_id,
                        Transacao.hash_dedup == hash_dedup,
                        Transacao.deleted_at.is_(None),
                    )
                )
                if existing.scalar_one_or_none():
                    duplicadas += 1
                    continue

                transacao = Transacao(
                    empresa_id=self._empresa_id,
                    agencia_id=agencia_id,
                    data=t.data,
                    valor=abs(t.valor),
                    historico=t.historico or t.tipo_ofx,
                    dc=dc,
                    saldo_apos=t.saldo_apos,   # OFX não informa: fica NULL
                    ordem=t.ordem,
                    importacao_id=importacao_id,
                    hash_dedup=hash_dedup,
                    status="pendente",
                )
                self._db.add(transacao)
                novas.append(transacao)
                hashes_do_lote.add(hash_dedup)
                importadas += 1
            except Exception as exc:
                logger.warning("extrato.transacao.erro", erro=str(exc))
                erros += 1

        await self._db.flush()

        logger.info(
            "extrato.importado",
            empresa_id=str(self._empresa_id),
            agencia_id=str(agencia_id),
            total=parse_result.total_blocos,
            importadas=importadas,
            duplicadas=duplicadas,
            erros=erros,
            rejeitadas=rejeitadas,
        )

        return ImportacaoResult(
            agencia_id=agencia_id,
            total_no_arquivo=parse_result.total_blocos,
            importadas=importadas,
            duplicadas=duplicadas,
            erros=erros,
            rejeitadas=rejeitadas,
            motivos_rejeicao=motivos_rejeicao,
            transacoes=[TransacaoResponse.model_validate(t) for t in novas],
        )

    async def importar_transacoes_raw(
        self,
        transacoes_raw: list,
        agencia_id: UUID,
        importacao_id: UUID | None = None,
    ) -> ImportacaoResult:
        """Persiste transações já parseadas (ex.: vindas do PDF parser)."""
        await self._get_agencia_or_400(agencia_id)

        if not transacoes_raw:
            from src.core.errors import ValidationError
            raise ValidationError(message="Nenhuma transação encontrada no PDF.")

        importadas, duplicadas, erros = 0, 0, 0
        rejeitadas, motivos_rejeicao = 0, []
        novas: list[Transacao] = []
        # Guarda hashes já vistos neste lote para evitar colisão de UniqueConstraint
        # quando o mesmo PDF tem transações legítimas mas com hash idêntico (ex.:
        # múltiplos boletos do mesmo fornecedor, mesmo valor, mesma data).
        hashes_do_lote: set[str] = set()

        for t in transacoes_raw:
            try:
                motivo = motivo_valor_nao_confiavel(t.historico, t.valor)
                if motivo:
                    # Não persiste: valor errado entra em silêncio e contamina o
                    # razão, enquanto a linha faltando aparece na conciliação.
                    rejeitadas += 1
                    motivos_rejeicao.append(f"{t.historico[:80]} — {motivo}")
                    logger.warning(
                        "extrato.transacao_rejeitada_valor_suspeito",
                        historico=t.historico[:200],
                        valor=str(t.valor),
                        motivo=motivo,
                    )
                    continue
                if historico_parece_linha_crua(t.historico):
                    logger.warning(
                        "extrato.historico_linha_crua",
                        historico=t.historico[:200],
                    )

                dc = "C" if t.valor >= 0 else "D"
                hash_dedup = _calcular_hash(self._empresa_id, agencia_id, t)

                # 1. Duplicata dentro do lote atual
                if hash_dedup in hashes_do_lote:
                    duplicadas += 1
                    continue

                # 2. Duplicata já persistida no banco
                # Só linha VIVA duplica. Linha cancelada mantém o hash no
                # banco (soft delete) e, sem este filtro, bloqueava para
                # sempre a reimportação do arquivo que o próprio usuário
                # cancelou — ver migration 0033.
                existing = await self._db.execute(
                    select(Transacao).where(
                        Transacao.empresa_id == self._empresa_id,
                        Transacao.hash_dedup == hash_dedup,
                        Transacao.deleted_at.is_(None),
                    )
                )
                if existing.scalar_one_or_none():
                    duplicadas += 1
                    continue

                transacao = Transacao(
                    empresa_id=self._empresa_id,
                    agencia_id=agencia_id,
                    data=t.data,
                    valor=abs(t.valor),
                    historico=t.historico or "EXTRATO PDF",
                    dc=dc,
                    saldo_apos=t.saldo_apos,
                    ordem=t.ordem,
                    importacao_id=importacao_id,
                    hash_dedup=hash_dedup,
                    status="pendente",
                )
                self._db.add(transacao)
                novas.append(transacao)
                hashes_do_lote.add(hash_dedup)
                importadas += 1
            except Exception as exc:
                logger.warning("extrato.pdf.transacao.erro", erro=str(exc))
                erros += 1

        await self._db.flush()

        return ImportacaoResult(
            agencia_id=agencia_id,
            total_no_arquivo=len(transacoes_raw),
            importadas=importadas,
            duplicadas=duplicadas,
            erros=erros,
            rejeitadas=rejeitadas,
            motivos_rejeicao=motivos_rejeicao,
            transacoes=[TransacaoResponse.model_validate(t) for t in novas],
        )

    async def listar(
        self,
        filtro: TransacaoFiltro,
        page: int = 1,
        page_size: int = 50,
    ) -> ExtratoPendentesResponse:
        q = select(Transacao).where(
            Transacao.empresa_id == self._empresa_id,
            # Sem isto, transação apagada continuava listada — e a limpeza de
            # extrato, que é soft delete, não escondia nada da tela.
            Transacao.deleted_at.is_(None),
        )

        if filtro.status:
            q = q.where(Transacao.status == filtro.status)
        if filtro.agencia_id:
            q = q.where(Transacao.agencia_id == filtro.agencia_id)
        if filtro.data_de:
            q = q.where(Transacao.data >= filtro.data_de)
        if filtro.data_ate:
            q = q.where(Transacao.data <= filtro.data_ate)
        if filtro.dc:
            q = q.where(Transacao.dc == filtro.dc)
        if filtro.valor_min is not None:
            q = q.where(Transacao.valor >= filtro.valor_min)
        if filtro.valor_max is not None:
            q = q.where(Transacao.valor <= filtro.valor_max)
        if filtro.historico:
            # `valor` é sempre positivo na tabela; o sinal mora em `dc`. Por isso
            # a faixa de valor não precisa de abs() aqui.
            termo = f"%{filtro.historico.strip()}%"
            q = q.where(Transacao.historico.ilike(termo))

        count_q = select(func.count()).select_from(q.subquery())
        total = (await self._db.execute(count_q)).scalar_one()

        rows = (
            await self._db.execute(
                # Ordem de leitura do extrato — a mesma da exportação, definida
                # em `ordenacao.py`. A contagem acima roda sobre `q` sem o join
                # do lote de propósito: total não depende de ordem.
                ordenar_como_o_extrato(q)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        return ExtratoPendentesResponse(
            items=[TransacaoResponse.model_validate(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def obter(self, transacao_id: UUID) -> TransacaoResponse:
        result = await self._db.execute(
            select(Transacao).where(
                Transacao.id == transacao_id,
                Transacao.empresa_id == self._empresa_id,
                Transacao.deleted_at.is_(None),
            )
        )
        t = result.scalar_one_or_none()
        if not t:
            raise NotFoundError(message="Transação não encontrada.")
        return TransacaoResponse.model_validate(t)

    async def _get_agencia_or_400(self, agencia_id: UUID) -> AgenciaBancaria:
        result = await self._db.execute(
            select(AgenciaBancaria).where(
                AgenciaBancaria.id == agencia_id,
                AgenciaBancaria.empresa_id == self._empresa_id,
                AgenciaBancaria.deleted_at == None,
            )
        )
        agencia = result.scalar_one_or_none()
        if not agencia:
            raise ValidationError(message="Agência bancária não encontrada nesta empresa.")
        return agencia


def _calcular_hash(empresa_id: UUID, agencia_id: UUID, t: TransacaoOFX) -> str:
    """SHA-256 determinístico — base para deduplicação idempotente.

    PDFs (fitid começa com 'PDF'):
        Usa o fitid completo no hash.  O fitid já embute a posição da transação
        no arquivo (idx) via MD5(data+historico+valor+idx), então duas transações
        com mesma descrição e mesmo valor em datas iguais (ex: boletos do mesmo
        fornecedor) terão fitids distintos → hashes distintos → sem colisão de
        UniqueConstraint ao inserir o lote inteiro.

        Deduplicação cross-import é garantida porque o pdfplumber e o OCR com
        temperature=0 são determinísticos: mesmo PDF → mesma sequência de fitids.

    OFX:
        FITID vem do banco e é a identidade estável. Data, valor e histórico
        podem mudar numa reexportação e não participam da deduplicação.
    """
    is_pdf = (t.fitid or "").startswith("PDF")

    if is_pdf:
        chave = json.dumps(
            {
                "empresa_id": str(empresa_id),
                "agencia_id": str(agencia_id),
                "fitid": t.fitid,          # já encoda data + historico + valor + idx
            },
            sort_keys=True,
        )
    else:
        chave = json.dumps(
            {
                "empresa_id": str(empresa_id),
                "agencia_id": str(agencia_id),
                "fitid": t.fitid,
            },
            sort_keys=True,
        )
    return hashlib.sha256(chave.encode()).hexdigest()
