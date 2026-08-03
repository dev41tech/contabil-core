"""Motor NEO — Matching automático de transações com regras.

Estratégias de match (em ordem de prioridade):
  1. exato      — historico da transação == historico da regra (case-insensitive)
  2. substring  — historico da regra é substring do historico da transação
  3. prefixo    — historico da transação começa com o historico da regra

Desempate entre regras candidatas:
  Nas estratégias `substring` e `prefixo` mais de uma regra pode casar. Vence a
  mais específica — o histórico mais longo — e, em caso de empate, o menor `id`.
  A ordem vem do `ORDER BY` em `_carregar_regras`, não da ordem que o banco
  devolveu, para que o mesmo extrato produza sempre a mesma classificação.

Ao encontrar match:
  - Cria RegistroContabil vinculado à transação.
  - Atualiza status da transação para "processada".
  - Salva NeoDecisao com a estratégia usada.
  - Tenta auto-associar Comprovantes e NotasFiscais com valor/data próximos.

Ao não encontrar match:
  - Salva NeoDecisao com resultado "sem_regra".
  - Transação permanece "pendente".

Idempotência:
  - Transações com status != "pendente" são ignoradas.
  - Re-executar o NEO na mesma empresa é seguro.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Comprovante, NeoDecisao, NotaFiscal, Regra, RegistroContabil, Transacao
from src.schemas.neo import NeoResultado

logger = structlog.get_logger(__name__)

# Tolerâncias para auto-associação
_VALOR_TOLERANCIA = Decimal("0.01")   # diferença máxima de valor (R$)
_DATA_TOLERANCIA_COMP = 3             # dias de tolerância para comprovantes
_DATA_TOLERANCIA_NF = 7              # dias de tolerância para notas fiscais


class NeoEngine:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def processar(self, agencia_id: UUID | None = None) -> NeoResultado:
        """Processa todas as transações pendentes da empresa (ou de uma agência específica)."""
        regras = await self._carregar_regras(agencia_id)
        pendentes = await self._carregar_pendentes(agencia_id)

        associadas = sem_regra = erros = 0
        comprovantes_associados = notas_associadas = 0

        for transacao in pendentes:
            try:
                regra, estrategia = self._encontrar_regra(transacao, regras)
                if regra:
                    await self._registrar_match(transacao, regra, estrategia)
                    associadas += 1
                    # Auto-associação de comprovantes e notas fiscais (tasks 5 & 6)
                    comp = await self._tentar_associar_comprovante(transacao)
                    if comp:
                        comprovantes_associados += 1
                    nf = await self._tentar_associar_nota_fiscal(transacao)
                    if nf:
                        notas_associadas += 1
                else:
                    await self._registrar_sem_regra(transacao)
                    sem_regra += 1
            except Exception as exc:
                logger.error(
                    "neo.erro_transacao",
                    transacao_id=str(transacao.id),
                    erro=str(exc),
                )
                await self._registrar_erro(transacao, str(exc))
                erros += 1

        await self._db.flush()

        logger.info(
            "neo.processado",
            empresa_id=str(self._empresa_id),
            total=len(pendentes),
            associadas=associadas,
            sem_regra=sem_regra,
            erros=erros,
            comprovantes_associados=comprovantes_associados,
            notas_associadas=notas_associadas,
        )

        return NeoResultado(
            empresa_id=self._empresa_id,
            total_pendentes=len(pendentes),
            associadas=associadas,
            sem_regra=sem_regra,
            erros=erros,
            comprovantes_associados=comprovantes_associados,
            notas_associadas=notas_associadas,
            processado_em=datetime.now(UTC),
        )

    # ── Matching ──────────────────────────────────────────────────────────────

    def _encontrar_regra(
        self, transacao: Transacao, regras: list[Regra]
    ) -> tuple[Regra | None, str | None]:
        """Tenta as estratégias em ordem de precisão."""
        historico_t = transacao.historico.lower().strip()

        # Filtra regras compatíveis com a agência e D/C da transação
        candidatas = [
            r for r in regras
            if r.agencia_id == transacao.agencia_id and r.dc == transacao.dc
        ]

        # 1. Match exato
        for regra in candidatas:
            if historico_t == regra.historico.lower().strip():
                return regra, "exato"

        # 2. Match por substring (regra dentro da transação)
        for regra in candidatas:
            if regra.historico.lower().strip() in historico_t:
                return regra, "substring"

        # 3. Match por prefixo (transação começa com o histórico da regra)
        for regra in candidatas:
            if historico_t.startswith(regra.historico.lower().strip()):
                return regra, "prefixo"

        return None, None

    # ── Persistência ──────────────────────────────────────────────────────────

    async def _registrar_match(
        self, transacao: Transacao, regra: Regra, estrategia: str
    ) -> None:
        historico_saida = (
            transacao.historico if regra.manter_historico else regra.descricao
        )

        registro = RegistroContabil(
            empresa_id=self._empresa_id,
            transacao_id=transacao.id,
            conta_id=regra.conta_id,
            agencia_id=transacao.agencia_id,
            descricao=regra.descricao,
            historico=historico_saida,
            historico_extrato=transacao.historico,
            dc=regra.dc,
            tipo_regra=regra.tipo,
            valor=transacao.valor,
            data_lancamento=transacao.data,
        )
        self._db.add(registro)

        transacao.status = "processada"

        decisao = NeoDecisao(
            empresa_id=self._empresa_id,
            transacao_id=transacao.id,
            regra_id=regra.id,
            resultado="associada",
            estrategia=estrategia,
            motivo=f"Regra '{regra.historico}' ({estrategia})",
        )
        self._db.add(decisao)

    async def _registrar_sem_regra(self, transacao: Transacao) -> None:
        decisao = NeoDecisao(
            empresa_id=self._empresa_id,
            transacao_id=transacao.id,
            regra_id=None,
            resultado="sem_regra",
            estrategia=None,
            motivo=f"Nenhuma regra encontrada para '{transacao.historico}' (dc={transacao.dc})",
        )
        self._db.add(decisao)

    async def _registrar_erro(self, transacao: Transacao, erro: str) -> None:
        transacao.status = "erro"
        decisao = NeoDecisao(
            empresa_id=self._empresa_id,
            transacao_id=transacao.id,
            regra_id=None,
            resultado="erro",
            estrategia=None,
            motivo=erro[:500],
        )
        self._db.add(decisao)

    # ── Auto-associação de comprovantes (Task 5) ───────────────────────────────

    async def _tentar_associar_comprovante(self, transacao: Transacao) -> bool:
        """Vincula automaticamente um comprovante à transação por valor e data.

        Critérios:
          - Mesmo empresa_id
          - transacao_id IS NULL (ainda não associado)
          - |valor_pago - transacao.valor| <= R$ 0,01
          - |data_pagamento - transacao.data| <= 3 dias

        Só associa se houver EXATAMENTE 1 match (evita falso positivos).
        Retorna True se associou, False caso contrário.
        """
        try:
            data_min = transacao.data - timedelta(days=_DATA_TOLERANCIA_COMP)
            data_max = transacao.data + timedelta(days=_DATA_TOLERANCIA_COMP)
            valor = Decimal(str(transacao.valor))

            q = select(Comprovante).where(
                and_(
                    Comprovante.empresa_id == self._empresa_id,
                    Comprovante.transacao_id.is_(None),
                    Comprovante.data_pagamento >= data_min,
                    Comprovante.data_pagamento <= data_max,
                )
            )
            candidatos = (await self._db.execute(q)).scalars().all()

            # Filtra por tolerância de valor (em Python para evitar erros de float no SQL)
            matches = [
                c for c in candidatos
                if abs(Decimal(str(c.valor_pago)) - valor) <= _VALOR_TOLERANCIA
            ]

            if len(matches) != 1:
                # 0 = não encontrou; 2+ = ambíguo; ambos são descartados
                return False

            matches[0].transacao_id = transacao.id
            logger.info(
                "neo.comprovante_associado",
                comprovante_id=str(matches[0].id),
                transacao_id=str(transacao.id),
            )
            return True

        except Exception as exc:
            logger.warning("neo.auto_comprovante.erro", transacao_id=str(transacao.id), erro=str(exc))
            return False

    # ── Auto-associação de notas fiscais (Task 6) ─────────────────────────────

    async def _tentar_associar_nota_fiscal(self, transacao: Transacao) -> bool:
        """Vincula automaticamente uma nota fiscal à transação por valor e data.

        Critérios:
          - Mesmo empresa_id
          - transacao_id IS NULL (ainda não associada)
          - status = 'pendente'
          - |valor - transacao.valor| <= R$ 0,01
          - |data_emissao - transacao.data| <= 7 dias

        Só associa se houver EXATAMENTE 1 match.
        Retorna True se associou, False caso contrário.
        """
        try:
            data_min = transacao.data - timedelta(days=_DATA_TOLERANCIA_NF)
            data_max = transacao.data + timedelta(days=_DATA_TOLERANCIA_NF)
            valor = Decimal(str(transacao.valor))

            q = select(NotaFiscal).where(
                and_(
                    NotaFiscal.empresa_id == self._empresa_id,
                    NotaFiscal.transacao_id.is_(None),
                    NotaFiscal.status == "pendente",
                    NotaFiscal.data_emissao >= data_min,
                    NotaFiscal.data_emissao <= data_max,
                )
            )
            candidatas = (await self._db.execute(q)).scalars().all()

            matches = [
                nf for nf in candidatas
                if abs(Decimal(str(nf.valor)) - valor) <= _VALOR_TOLERANCIA
            ]

            if len(matches) != 1:
                return False

            matches[0].transacao_id = transacao.id
            matches[0].status = "associada"
            logger.info(
                "neo.nota_associada",
                nota_id=str(matches[0].id),
                transacao_id=str(transacao.id),
            )
            return True

        except Exception as exc:
            logger.warning("neo.auto_nota.erro", transacao_id=str(transacao.id), erro=str(exc))
            return False

    # ── Queries ───────────────────────────────────────────────────────────────

    async def _carregar_regras(self, agencia_id: UUID | None) -> list[Regra]:
        """Carrega as regras já na ordem em que o matching deve considerá-las.

        A ordem é parte do resultado: nas estratégias `substring` e `prefixo` a
        primeira regra que casar vence. Sem `ORDER BY`, a ordem é a que o Postgres
        devolveu — e a mesma transação pode cair em contas diferentes entre
        execuções. Ordenamos pela regra mais específica (histórico mais longo) e
        desempatamos por `id` para o resultado ser sempre o mesmo.
        """
        q = (
            select(Regra)
            .where(
                Regra.empresa_id == self._empresa_id,
                Regra.ativa == True,
                Regra.tipo == "automatica",
            )
            .order_by(func.length(Regra.historico).desc(), Regra.id.asc())
        )
        if agencia_id:
            q = q.where(Regra.agencia_id == agencia_id)
        return (await self._db.execute(q)).scalars().all()

    async def _carregar_pendentes(self, agencia_id: UUID | None) -> list[Transacao]:
        """Carrega as transações pendentes em ordem estável.

        Importa mesmo sem empate de regra: a auto-associação de comprovantes e
        notas fiscais consome candidatos do pool, então quem é processado antes
        fica com o comprovante disputado.
        """
        q = (
            select(Transacao)
            .where(
                Transacao.empresa_id == self._empresa_id,
                Transacao.status == "pendente",
            )
            .order_by(Transacao.data.asc(), Transacao.id.asc())
        )
        if agencia_id:
            q = q.where(Transacao.agencia_id == agencia_id)
        return (await self._db.execute(q)).scalars().all()
