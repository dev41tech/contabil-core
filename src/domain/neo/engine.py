"""Motor NEO — Matching automático de transações com regras.

Estratégias de match (em ordem de prioridade). Os dois lados são comparados
na forma canônica de `src.core.texto.normalizar_para_match` — minúsculas, sem
acento, sem pontuação, espaços colapsados:
  1. exato          — historico da transação == historico da regra
  2. substring      — historico da regra é substring do historico da transação
  3. todas_palavras — todas as palavras da regra aparecem no histórico, em
                      qualquer ordem e não necessariamente coladas

Desempate entre regras candidatas:
  Nas estratégias `substring` e `todas_palavras` mais de uma regra
  pode casar. Vence a mais específica — o histórico mais longo — e, em caso de
  empate, o menor `id`.
  A ordem vem do `ORDER BY` em `_carregar_regras`, não da ordem que o banco
  devolveu, para que o mesmo extrato produza sempre a mesma classificação.

Ao encontrar match:
  - Cria um lançamento contábil com classificação e contrapartida bancária.
  - Atualiza status da transação para "processada".
  - Salva NeoDecisao com a estratégia usada.
  - Tenta auto-associar Comprovantes e NotasFiscais com valor/data próximos.

Classificação por contraparte (itens 1+2 do PDF de feedback dos contadores):
  Quando NENHUMA regra casa, antes de desistir o motor tenta achar a
  contraparte (fornecedor/cliente cadastrado) por duas vias, nesta ordem:
  primeiro o CNPJ/CPF do comprovante ou nota fiscal candidato; depois, se isso
  não resolver, o NOME da contraparte dentro do histórico do extrato. Nome é
  evidência mais fraca que documento e recusa em qualquer ambiguidade — ver
  `contraparte_por_nome.py`. Se achar, classifica exatamente como um
  match de regra faria — conta da contraparte, histórico no formato "PGTO/
  RECEBIMENTO REF [NF ...] RAZÃO SOCIAL" — com `estrategia="contraparte"` e
  `regra_id=None`. Isso NUNCA disputa com uma regra já existente: só entra em
  jogo quando a regra não classificou nada, então não pode regredir nenhuma
  classificação que já funcionava.

Ao não encontrar match nem contraparte:
  - Salva NeoDecisao com resultado "sem_regra".
  - Transação permanece "pendente".

Idempotência:
  - Transações com status != "pendente" são ignoradas.
  - Re-executar o NEO na mesma empresa é seguro.

Seleção vs. vinculação de comprovante/nota fiscal:
  `_selecionar_*_candidato` só consulta (com lock) e nunca muta `transacao_id`;
  `_vincular_*` faz a mutação depois que as partidas já foram criadas. A
  separação existe para permitir que uma entrega futura resolva a
  contraparte (CNPJ/nome do candidato) antes de decidir conta e histórico,
  sem se arriscar a associar um documento a uma transação cujo lançamento
  falhou no meio do caminho.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.dates import bounds_do_mes_data
from src.core.texto import normalizar_historico_contabil, normalizar_para_match
from src.domain.neo.contraparte_por_nome import (
    CandidataPorNome,
    casar_por_nome,
    nucleo_do_nome,
)
from src.db.models import (
    AgenciaBancaria,
    Comprovante,
    Contraparte,
    Empresa,
    NeoDecisao,
    NotaFiscal,
    PlanoConta,
    Regra,
    RegistroContabil,
    Transacao,
)
from src.schemas.neo import NeoResultado

logger = structlog.get_logger(__name__)

# Tolerâncias para auto-associação
_VALOR_TOLERANCIA = Decimal("0.01")   # diferença máxima de valor (R$)
_DATA_TOLERANCIA_COMP = 3             # dias de tolerância para comprovantes
_DATA_TOLERANCIA_NF = 7              # dias de tolerância para notas fiscais

_ESTRATEGIAS_MATCH = ("exato", "substring", "todas_palavras")
_ERROS_DE_PROGRAMACAO = (AttributeError, TypeError, NameError, ImportError, KeyError)


def estrategia_de_match_normalizado(regra: str, transacao: str) -> str | None:
    """Como uma regra casa, com os dois lados **já** em forma canônica.

    Fonte única das estratégias usadas pelo motor e pela prévia de impacto —
    duas definições de match fariam a prévia mentir sobre o que o motor vai
    fazer. A ordem entre várias regras continua sendo de `_encontrar_regra`,
    que depende da lista ordenada vinda do banco.

    Recebe texto já normalizado, e não cru, porque o motor compara cada
    transação contra todas as regras candidatas: normalizar aqui dentro
    refaria o trabalho sobre o mesmo histórico de regra a cada par. Medido em
    50 regras × 1000 transações, a versão que normalizava por par ficou ~38x
    mais lenta. Quem tem só um par na mão usa `estrategia_de_match`.
    """
    if transacao == regra:
        return "exato"
    if regra in transacao:
        return "substring"
    tokens_regra = regra.split()
    if tokens_regra:
        tokens_transacao = set(transacao.split())
        if all(token in tokens_transacao for token in tokens_regra):
            return "todas_palavras"
    return None


def estrategia_de_match(historico_regra: str, historico_transacao: str) -> str | None:
    """Versão conveniente para texto cru — usada pela simulação de regra, que
    avalia um histórico de regra por vez."""
    return estrategia_de_match_normalizado(
        normalizar_para_match(historico_regra),
        normalizar_para_match(historico_transacao),
    )


@dataclass(frozen=True)
class ResolucaoSombra:
    """Resultado — só leitura — de tentar resolver a contraparte de uma
    transação já classificada por regra. Usado para medir cobertura e
    conflito com as regras atuais antes de a Entrega 6/7 ativar isso de
    verdade; nunca é aplicado à transação."""

    contraparte_id: UUID
    documento: str
    origem_evidencia: str  # "nota_fiscal" | "comprovante" | "nome"
    conta_contraparte_id: UUID
    conta_divergente: bool
    historico_sugerido: str


def gerar_historico_sugerido(dc: str, razao_social: str, numero_nf: str | None) -> str:
    """Histórico no formato pedido pelos contadores (item 2 do PDF de
    feedback): 'PGTO/RECEBIMENTO REF [NF XXX –] RAZÃO SOCIAL'.
    """
    prefixo = "PGTO" if dc == "D" else "RECEBIMENTO"
    numero_nf = (numero_nf or "").strip()
    texto = (
        f"{prefixo} REF NF {numero_nf} - {razao_social}"
        if numero_nf
        else f"{prefixo} REF {razao_social}"
    )
    return normalizar_historico_contabil(texto)


class NeoEngine:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id
        self._comprovantes_consumidos: set[UUID] = set()
        self._notas_consumidas: set[UUID] = set()
        self._contas_bancarias: dict[UUID, PlanoConta] = {}
        self._regras_normalizadas: dict[UUID, str] = {}
        self._decisoes_sem_regra: dict[UUID, NeoDecisao] = {}
        self._empresa_cnpj: str | None = None

    async def processar(
        self,
        agencia_id: UUID | None = None,
        mes: str | None = None,
        progresso: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> NeoResultado:
        """Processa as transações pendentes da empresa (opcionalmente restrito a
        uma agência e/ou a um mês específico, formato 'AAAA-MM').

        ``progresso`` recebe (processados, total), sem conhecer persistência ou
        o modelo Job. Assim o motor continua reutilizável em rotas e testes, e
        quem o orquestra decide quando agrupar escritas no banco.
        """
        regras = await self._carregar_regras(agencia_id)
        pendentes = await self._carregar_pendentes(agencia_id, mes)
        if progresso is not None:
            await progresso(0, len(pendentes))
        self._decisoes_sem_regra = await self._carregar_decisoes_sem_regra(pendentes)
        self._comprovantes_consumidos.clear()
        self._notas_consumidas.clear()
        self._empresa_cnpj = (
            await self._db.execute(
                select(Empresa.cnpj).where(Empresa.id == self._empresa_id)
            )
        ).scalar_one()

        associadas = sem_regra = erros = 0
        comprovantes_associados = notas_associadas = 0
        classificadas_por_contraparte = 0

        for indice, transacao in enumerate(pendentes, start=1):
            try:
                teve_match = associou_comprovante = associou_nota = False
                associou_por_contraparte = False
                async with self._db.begin_nested():
                    regra, estrategia = self._encontrar_regra(transacao, regras)
                    if regra:
                        associou_comprovante, associou_nota = await self._registrar_match(
                            transacao, regra, estrategia
                        )
                        teve_match = True
                    else:
                        resultado_contraparte = await self._tentar_classificar_por_contraparte(
                            transacao
                        )
                        if resultado_contraparte is not None:
                            associou_comprovante, associou_nota = resultado_contraparte
                            teve_match = True
                            associou_por_contraparte = True
                        elif transacao.id not in self._decisoes_sem_regra:
                            await self._registrar_sem_regra(transacao)
                    await self._db.flush()
                if teve_match:
                    associadas += 1
                    comprovantes_associados += int(associou_comprovante)
                    notas_associadas += int(associou_nota)
                    if associou_por_contraparte:
                        classificadas_por_contraparte += 1
                else:
                    sem_regra += 1
            except Exception as exc:
                # Estes tipos apontam para contrato quebrado no próprio código, não
                # para uma transação ruim. Convertê-los em decisão `erro` esconderia
                # a causa e repetiria o mesmo defeito em todo o extrato. Se um parser
                # tiver um caso legítimo desses, ele deve capturá-lo na fronteira e
                # traduzi-lo para uma exceção de domínio antes de chegar ao motor.
                if isinstance(exc, _ERROS_DE_PROGRAMACAO):
                    raise
                logger.error(
                    "neo.erro_transacao",
                    transacao_id=str(transacao.id),
                    erro=str(exc),
                )
                await self._registrar_erro(transacao, str(exc))
                await self._db.flush()
                erros += 1
            if progresso is not None:
                await progresso(indice, len(pendentes))

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
            classificadas_por_contraparte=classificadas_por_contraparte,
        )

        return NeoResultado(
            empresa_id=self._empresa_id,
            total_pendentes=len(pendentes),
            associadas=associadas,
            sem_regra=sem_regra,
            erros=erros,
            comprovantes_associados=comprovantes_associados,
            notas_associadas=notas_associadas,
            classificadas_por_contraparte=classificadas_por_contraparte,
            processado_em=datetime.now(UTC),
        )

    # ── Matching ──────────────────────────────────────────────────────────────

    def _forma_de_match(self, regra: Regra) -> str:
        """Forma canônica do histórico da regra, memoizada por execução.

        Não usa `regra.historico_normalizado` (que é só `strip().lower()`, e
        existe para o índice único de unicidade) porque o matching precisa
        ignorar também acento e pontuação. Memoizar aqui importa: cada
        transação é comparada contra todas as regras candidatas, então sem
        isto o mesmo histórico de regra seria normalizado uma vez por
        transação.
        """
        cacheado = self._regras_normalizadas.get(regra.id)
        if cacheado is None:
            cacheado = normalizar_para_match(regra.historico)
            self._regras_normalizadas[regra.id] = cacheado
        return cacheado

    def _encontrar_regra(
        self, transacao: Transacao, regras: list[Regra]
    ) -> tuple[Regra | None, str | None]:
        """Tenta as estratégias em ordem de precisão.

        Todas as comparações acontecem na forma canônica dos dois lados
        (minúsculas, sem acento, sem pontuação, espaços colapsados), então
        uma regra "TARIFA" reconhece "Tarifa com liquidação", "TARIFA COM
        LIQUIDACAO" e "TARIFA/LIQUIDAÇÃO" sem precisar de uma regra por
        variação.
        """
        # Filtra regras compatíveis com a agência e D/C da transação
        candidatas = [
            r for r in regras
            if r.agencia_id == transacao.agencia_id and r.dc == transacao.dc
        ]

        # Estratégia vence antes da ordem das regras: primeiro todas as exatas,
        # depois substrings e por fim todas_palavras. A função pura chamada
        # aqui também sustenta a simulação, evitando duas definições de match.
        historico_t = normalizar_para_match(transacao.historico)
        matches = {
            regra.id: estrategia_de_match_normalizado(
                self._forma_de_match(regra), historico_t
            )
            for regra in candidatas
        }
        for estrategia in _ESTRATEGIAS_MATCH:
            for regra in candidatas:
                if matches[regra.id] == estrategia:
                    return regra, estrategia

        return None, None

    # ── Persistência ──────────────────────────────────────────────────────────

    async def _registrar_match(
        self, transacao: Transacao, regra: Regra, estrategia: str
    ) -> tuple[bool, bool]:
        """Cria a classificação da transação e tenta vincular comprovante/nota fiscal.

        A *seleção* dos candidatos (consulta com lock, sem mutar nada) acontece
        antes da criação das partidas — não porque a ordem de escrita importe
        para o comportamento atual, mas para deixar disponível a informação do
        documento (CNPJ/nome) que uma entrega futura vai usar para resolver a
        conta/histórico por contraparte. A *vinculação* (mutação de
        `transacao_id`) só acontece depois que as partidas já existem, como
        sempre foi — se algo falhar antes disso, o savepoint do chamador
        desfaz tudo e nenhum documento fica associado a uma transação sem
        lançamento.
        """
        historico_saida = (
            transacao.historico if regra.manter_historico else regra.descricao
        )

        comprovante_candidato = await self._selecionar_comprovante_candidato(transacao)
        nota_candidata = await self._selecionar_nota_candidata(transacao)

        resolucao_sombra = await self._logar_resolucao_sombra(
            transacao, regra, historico_saida, comprovante_candidato, nota_candidata
        )

        await self._registrar_partidas(
            transacao=transacao,
            conta_id=regra.conta_id,
            descricao=regra.descricao,
            historico=historico_saida,
            dc=regra.dc,
            tipo_regra=regra.tipo,
        )

        transacao.status = "processada"

        self._registrar_decisao(
            transacao,
            resultado="associada",
            regra_id=regra.id,
            conta_id=regra.conta_id,
            estrategia=estrategia,
            motivo=f"Regra '{regra.historico}' ({estrategia})",
            resolucao_sombra=resolucao_sombra,
        )

        associou_comprovante = False
        if comprovante_candidato is not None:
            await self._vincular_comprovante(transacao, comprovante_candidato)
            associou_comprovante = True

        associou_nota = False
        if nota_candidata is not None:
            await self._vincular_nota(transacao, nota_candidata)
            associou_nota = True

        return associou_comprovante, associou_nota

    # ── Contraparte: shadow mode (quando já há regra) + classificação real
    #    (quando não há regra) ─────────────────────────────────────────────────
    #
    # Quando uma regra já classificou a transação, a resolução de contraparte
    # abaixo continua em shadow mode — só mede divergência com a regra, nunca
    # substitui a decisão dela (`_logar_resolucao_sombra`/`_resolver_contraparte_sombra`).
    # Quando NÃO há regra, `_tentar_classificar_por_contraparte` usa a mesma
    # resolução para classificar de verdade — é aí que os itens 1+2 do PDF
    # de feedback realmente entram em produção.

    async def _logar_resolucao_sombra(
        self,
        transacao: Transacao,
        regra: Regra,
        historico_regra: str,
        comprovante_candidato: Comprovante | None,
        nota_candidata: NotaFiscal | None,
    ) -> ResolucaoSombra | None:
        """Resolve e registra a medição paralela sem aplicar sua conta.

        A resolução é devolvida para que `_registrar_decisao` persista os
        atributos na mesma linha do desfecho. Este método não altera partidas,
        transação ou regra: shadow continua sendo apenas observação.
        """
        resolucao = await self._resolver_contraparte_sombra(
            transacao, regra, comprovante_candidato, nota_candidata
        )
        if resolucao is None:
            return None

        logger.info(
            "neo.shadow.contraparte_encontrada",
            transacao_id=str(transacao.id),
            contraparte_id=str(resolucao.contraparte_id),
            documento=resolucao.documento,
            origem_evidencia=resolucao.origem_evidencia,
            conta_regra_id=str(regra.conta_id),
            conta_contraparte_id=str(resolucao.conta_contraparte_id),
            conta_divergente=resolucao.conta_divergente,
            historico_regra=historico_regra,
            historico_sugerido=resolucao.historico_sugerido,
        )
        if resolucao.conta_divergente:
            logger.warning(
                "neo.shadow.conta_divergente",
                transacao_id=str(transacao.id),
                contraparte_id=str(resolucao.contraparte_id),
                conta_regra_id=str(regra.conta_id),
                conta_contraparte_id=str(resolucao.conta_contraparte_id),
            )
        return resolucao

    async def _resolver_contraparte_sombra(
        self,
        transacao: Transacao,
        regra: Regra,
        comprovante_candidato: Comprovante | None,
        nota_candidata: NotaFiscal | None,
    ) -> ResolucaoSombra | None:
        """Tenta identificar a contraparte a partir dos candidatos já
        selecionados por `_registrar_match`, e compara a conta que ela sugere
        com a que a regra decidiu. Não muta nada.
        """
        encontrado = await self._resolver_contraparte_candidata(
            transacao, comprovante_candidato, nota_candidata
        )
        if encontrado is None:
            return None
        contraparte, origem_evidencia, numero_nf = encontrado

        return ResolucaoSombra(
            contraparte_id=contraparte.id,
            documento=contraparte.documento,
            origem_evidencia=origem_evidencia,
            conta_contraparte_id=contraparte.conta_contabil_id,
            conta_divergente=contraparte.conta_contabil_id != regra.conta_id,
            historico_sugerido=gerar_historico_sugerido(
                dc=transacao.dc, razao_social=contraparte.razao_social, numero_nf=numero_nf
            ),
        )

    async def _tentar_classificar_por_contraparte(
        self, transacao: Transacao
    ) -> tuple[bool, bool] | None:
        """Classifica de verdade uma transação sem regra, usando o cadastro de
        contrapartes (itens 1+2 do PDF de feedback dos contadores) — ativado
        em 2026-08-18, depois de rodar em shadow mode.

        Só chamado quando `_encontrar_regra` não achou nada, então nunca
        disputa com uma regra já existente. Retorna `None` se não achou
        contraparte (o chamador segue o fluxo `sem_regra` de sempre); se
        achou, cria a classificação exatamente como `_registrar_match` faria
        para uma regra e retorna `(associou_comprovante, associou_nota)`.
        """
        comprovante_candidato = await self._selecionar_comprovante_candidato(transacao)
        nota_candidata = await self._selecionar_nota_candidata(transacao)

        encontrado = await self._resolver_contraparte_candidata(
            transacao, comprovante_candidato, nota_candidata
        )
        if encontrado is None:
            return None
        contraparte, origem_evidencia, numero_nf = encontrado

        historico_gerado = gerar_historico_sugerido(
            dc=transacao.dc, razao_social=contraparte.razao_social, numero_nf=numero_nf
        )

        await self._registrar_partidas(
            transacao=transacao,
            conta_id=contraparte.conta_contabil_id,
            descricao=contraparte.razao_social,
            historico=historico_gerado,
            dc=transacao.dc,
            tipo_regra="contraparte",
        )
        transacao.status = "processada"

        self._registrar_decisao(
            transacao,
            resultado="associada",
            regra_id=None,
            conta_id=contraparte.conta_contabil_id,
            estrategia="contraparte",
            motivo=(
                f"Contraparte '{contraparte.razao_social}' identificada via "
                f"{origem_evidencia} (documento {contraparte.documento})"
            ),
        )
        logger.info(
            "neo.classificado_por_contraparte",
            transacao_id=str(transacao.id),
            contraparte_id=str(contraparte.id),
            origem_evidencia=origem_evidencia,
            conta_id=str(contraparte.conta_contabil_id),
        )

        associou_comprovante = False
        if comprovante_candidato is not None:
            await self._vincular_comprovante(transacao, comprovante_candidato)
            associou_comprovante = True

        associou_nota = False
        if nota_candidata is not None:
            await self._vincular_nota(transacao, nota_candidata)
            associou_nota = True

        return associou_comprovante, associou_nota

    async def _resolver_contraparte_candidata(
        self,
        transacao: Transacao,
        comprovante_candidato: Comprovante | None,
        nota_candidata: NotaFiscal | None,
    ) -> tuple[Contraparte, str, str | None] | None:
        """Núcleo comum da resolução de contraparte — usado tanto pelo shadow
        mode (quando há regra) quanto pela classificação real (quando não
        há): acha o documento (CNPJ/CPF) nos candidatos e busca a contraparte
        cadastrada. Não muta nada — quem chama decide o que fazer com o
        resultado.

        Ordem de evidência: nota fiscal candidata (documento do lado que é a
        contraparte, conforme a direção financeira) primeiro, comprovante
        candidato como fallback. Retorna `(contraparte, origem_evidencia,
        numero_nf)` ou `None`.
        """
        documento: str | None = None
        origem_evidencia: str | None = None
        numero_nf: str | None = None

        if nota_candidata is not None:
            direcao = self._direcao_nota(nota_candidata)
            if direcao == transacao.dc:
                documento = (
                    nota_candidata.cnpj_emitente
                    if direcao == "D"
                    else nota_candidata.cnpj_destinatario
                )
                origem_evidencia = "nota_fiscal"
                numero_nf = nota_candidata.numero

        if documento is None and comprovante_candidato is not None:
            if comprovante_candidato.cpf_cnpj:
                documento = comprovante_candidato.cpf_cnpj
                origem_evidencia = "comprovante"

        if documento is not None:
            digitos = self._somente_digitos(documento)
            if digitos:
                contraparte = await self._buscar_contraparte_por_documento(digitos)
                if contraparte is not None:
                    return contraparte, origem_evidencia, numero_nf
                logger.debug(
                    "neo.contraparte_nao_encontrada",
                    transacao_id=str(transacao.id),
                    documento=digitos,
                    origem_evidencia=origem_evidencia,
                )

        # Sem evidência documental, tenta o nome no histórico do extrato.
        #
        # Nome é evidência MAIS FRACA que documento: o CNPJ bate ou não bate,
        # enquanto o nome depende de como o banco escreveu. Por isso só entra
        # aqui, depois de o documento não ter resolvido, e recusa em qualquer
        # ambiguidade — pendente aparece na fila, classificado errado entra no
        # razão em silêncio.
        por_nome = await self._buscar_contraparte_por_nome(transacao)
        if por_nome is not None:
            return por_nome, "nome", numero_nf
        return None

    async def _buscar_contraparte_por_nome(
        self, transacao: Transacao
    ) -> Contraparte | None:
        """Casa razão social ou nome fantasia com o histórico do lançamento."""
        contrapartes = (
            (
                await self._db.execute(
                    select(Contraparte).where(
                        Contraparte.empresa_id == self._empresa_id,
                        Contraparte.ativa == True,  # noqa: E712
                        Contraparte.deleted_at.is_(None),
                        Contraparte.conta_contabil_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not contrapartes:
            return None

        candidatas: list[CandidataPorNome] = []
        por_id: dict[UUID, Contraparte] = {}
        for contraparte in contrapartes:
            por_id[contraparte.id] = contraparte
            for nome in (contraparte.razao_social, contraparte.nome_fantasia):
                nucleo = nucleo_do_nome(nome)
                if nucleo:
                    candidatas.append(
                        CandidataPorNome(contraparte_id=contraparte.id, nucleo=nucleo)
                    )

        achada, motivo_conflito = casar_por_nome(transacao.historico or "", candidatas)
        if achada is None:
            if motivo_conflito:
                # Quase-acerto recusado precisa virar texto na fila, senão a
                # transação parece simplesmente ignorada pelo motor.
                logger.info(
                    "neo.contraparte_por_nome_ambigua",
                    transacao_id=str(transacao.id),
                    motivo=motivo_conflito,
                )
            return None

        logger.info(
            "neo.contraparte_por_nome",
            transacao_id=str(transacao.id),
            contraparte_id=str(achada.contraparte_id),
            nucleo=achada.nucleo,
        )
        return por_id[achada.contraparte_id]

    async def _buscar_contraparte_por_documento(self, documento: str) -> Contraparte | None:
        result = await self._db.execute(
            select(Contraparte).where(
                Contraparte.empresa_id == self._empresa_id,
                Contraparte.documento == documento,
                Contraparte.ativa == True,
                Contraparte.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def registrar_partidas_manuais(
        self, transacao: Transacao, conta_id: UUID, descricao: str
    ) -> None:
        """Cria, atomicamente, a classificação manual e sua contrapartida bancária."""
        await self._registrar_partidas(
            transacao=transacao,
            conta_id=conta_id,
            descricao=descricao,
            historico=transacao.historico,
            dc=transacao.dc,
            tipo_regra="manual",
        )

    async def classificar_manualmente_lote(
        self, transacoes: list[Transacao], conta_id: UUID, descricao: str
    ) -> list[NeoDecisao]:
        """Classifica transações já travadas e encerra suas decisões abertas.

        O chamador seleciona e trava apenas pendências da empresa. Concentrar
        aqui partidas, status e decisão garante que os endpoints individual e
        em lote não possam deixar uma decisão `sem_regra` órfã.
        """
        self._decisoes_sem_regra = await self._carregar_decisoes_sem_regra(transacoes)
        decisoes: list[NeoDecisao] = []
        for transacao in transacoes:
            await self.registrar_partidas_manuais(transacao, conta_id, descricao)
            transacao.status = "processada"
            decisoes.append(
                self._registrar_decisao(
                    transacao,
                    resultado="associada",
                    regra_id=None,
                    conta_id=conta_id,
                    estrategia="manual",
                    motivo=f"Associação manual: {descricao}",
                )
            )
        return decisoes

    async def _registrar_partidas(
        self,
        transacao: Transacao,
        conta_id: UUID,
        descricao: str,
        historico: str,
        dc: str,
        tipo_regra: str,
    ) -> None:
        conta_bancaria = await self._obter_conta_bancaria(transacao.agencia_id)
        lancamento_id = uuid4()
        descricao_exibicao = normalizar_historico_contabil(descricao)
        dados_comuns = {
            "empresa_id": self._empresa_id,
            "transacao_id": transacao.id,
            "lancamento_id": lancamento_id,
            "agencia_id": transacao.agencia_id,
            "historico": normalizar_historico_contabil(historico),
            "historico_extrato": transacao.historico,
            "tipo_regra": tipo_regra,
            "valor": transacao.valor,
            "data_lancamento": transacao.data,
        }
        self._db.add(
            RegistroContabil(
                **dados_comuns,
                conta_id=conta_id,
                descricao=descricao_exibicao,
                dc=dc,
            )
        )
        self._db.add(
            RegistroContabil(
                **dados_comuns,
                conta_id=conta_bancaria.id,
                descricao=normalizar_historico_contabil(
                    f"Contrapartida bancária: {descricao}"
                ),
                dc="C" if dc == "D" else "D",
            )
        )

    async def _obter_conta_bancaria(self, agencia_id: UUID) -> PlanoConta:
        if agencia_id in self._contas_bancarias:
            return self._contas_bancarias[agencia_id]

        agencia = (
            await self._db.execute(
                select(AgenciaBancaria)
                .where(
                    AgenciaBancaria.id == agencia_id,
                    AgenciaBancaria.empresa_id == self._empresa_id,
                    AgenciaBancaria.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one()

        conta = None
        if agencia.conta_contabil_id:
            conta = (
                await self._db.execute(
                    select(PlanoConta).where(
                        PlanoConta.id == agencia.conta_contabil_id,
                        PlanoConta.empresa_id == self._empresa_id,
                        PlanoConta.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

        if conta is None:
            codigo = f"1.1.B.{agencia.id.hex[:16]}"
            conta = (
                await self._db.execute(
                    select(PlanoConta).where(
                        PlanoConta.empresa_id == self._empresa_id,
                        PlanoConta.codigo == codigo,
                        PlanoConta.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if conta is None:
                conta = PlanoConta(
                    empresa_id=self._empresa_id,
                    codigo=codigo,
                    descricao=f"Conta bancária {agencia.descricao}",
                    tipo="ativo",
                    tipo_sa="A",
                )
                self._db.add(conta)
                await self._db.flush()
            agencia.conta_contabil_id = conta.id

        self._contas_bancarias[agencia_id] = conta
        return conta

    def _registrar_decisao(
        self,
        transacao: Transacao,
        *,
        resultado: str,
        regra_id: UUID | None,
        conta_id: UUID | None,
        estrategia: str | None,
        motivo: str | None,
        resolucao_sombra: ResolucaoSombra | None = None,
    ) -> NeoDecisao:
        """Grava o desfecho do NEO para esta transação.

        Se já existe uma decisão 'sem_regra' aberta para a transação, ela é
        *encerrada* (atualizada no lugar) em vez de ficar para trás enquanto uma
        linha nova é inserida. Antes disso, uma transação que caía em 'sem_regra'
        numa execução e era classificada na seguinte deixava a linha antiga
        órfã: a tela "Sem Regra" continuava listando um lançamento já
        contabilizado e tentar associá-lo respondia "A transação já foi
        contabilizada ou não está mais pendente".
        """
        aberta = self._decisoes_sem_regra.get(transacao.id)
        if aberta is not None:
            aberta.regra_id = regra_id
            aberta.conta_id = conta_id
            aberta.resultado = resultado
            aberta.estrategia = estrategia
            aberta.motivo = motivo
            aberta.contraparte_id = (
                resolucao_sombra.contraparte_id if resolucao_sombra else None
            )
            aberta.conta_contraparte_id = (
                resolucao_sombra.conta_contraparte_id if resolucao_sombra else None
            )
            aberta.origem_evidencia = (
                resolucao_sombra.origem_evidencia if resolucao_sombra else None
            )
            aberta.conta_divergente = (
                resolucao_sombra.conta_divergente if resolucao_sombra else None
            )
            aberta.processado_em = datetime.now(UTC)
            return aberta

        decisao = NeoDecisao(
            empresa_id=self._empresa_id,
            transacao_id=transacao.id,
            regra_id=regra_id,
            conta_id=conta_id,
            resultado=resultado,
            estrategia=estrategia,
            motivo=motivo,
            contraparte_id=(
                resolucao_sombra.contraparte_id if resolucao_sombra else None
            ),
            conta_contraparte_id=(
                resolucao_sombra.conta_contraparte_id if resolucao_sombra else None
            ),
            origem_evidencia=(
                resolucao_sombra.origem_evidencia if resolucao_sombra else None
            ),
            conta_divergente=(
                resolucao_sombra.conta_divergente if resolucao_sombra else None
            ),
        )
        self._db.add(decisao)
        # A linha nova não entra em `_decisoes_sem_regra`: esse índice é só das já
        # persistidas quando a execução começou. Uma linha recém-inserida pode
        # ser desfeita pelo savepoint desta transação, e reaproveitá-la depois
        # (no `_registrar_erro`) escreveria num objeto que o rollback já
        # descartou — o registro de erro simplesmente sumiria.
        return decisao

    async def _registrar_sem_regra(self, transacao: Transacao) -> None:
        self._registrar_decisao(
            transacao,
            resultado="sem_regra",
            regra_id=None,
            conta_id=None,
            estrategia=None,
            motivo=f"Nenhuma regra encontrada para '{transacao.historico}' (dc={transacao.dc})",
        )

    async def _registrar_erro(self, transacao: Transacao, erro: str) -> None:
        transacao.status = "erro"
        self._registrar_decisao(
            transacao,
            resultado="erro",
            regra_id=None,
            conta_id=None,
            estrategia=None,
            motivo=erro[:500],
        )

    # ── Auto-associação de comprovantes (Task 5) ───────────────────────────────

    async def _selecionar_comprovante_candidato(
        self, transacao: Transacao
    ) -> Comprovante | None:
        """Localiza — sem associar — um comprovante candidato à transação.

        Critérios:
          - Mesmo empresa_id
          - Transação de débito (comprovante representa pagamento)
          - transacao_id IS NULL (ainda não associado)
          - |valor_pago - transacao.valor| <= R$ 0,01
          - |data_pagamento - transacao.data| <= 3 dias

        Só retorna candidato se houver EXATAMENTE 1 match (evita falsos
        positivos). Não muta nada — quem chama decide se e quando vincular
        via `_vincular_comprovante`.
        """
        if transacao.dc != "D":
            return None

        try:
            data_min = transacao.data - timedelta(days=_DATA_TOLERANCIA_COMP)
            data_max = transacao.data + timedelta(days=_DATA_TOLERANCIA_COMP)
            valor = Decimal(str(transacao.valor))

            q = select(Comprovante).where(
                and_(
                    Comprovante.empresa_id == self._empresa_id,
                    Comprovante.transacao_id.is_(None),
                    Comprovante.deleted_at.is_(None),
                    Comprovante.data_pagamento >= data_min,
                    Comprovante.data_pagamento <= data_max,
                )
            )
            if self._comprovantes_consumidos:
                q = q.where(Comprovante.id.not_in(self._comprovantes_consumidos))
            q = q.with_for_update(skip_locked=True)
            candidatos = (await self._db.execute(q)).scalars().all()

            # Filtra por tolerância de valor (em Python para evitar erros de float no SQL)
            matches = [
                c for c in candidatos
                if abs(Decimal(str(c.valor_pago)) - valor) <= _VALOR_TOLERANCIA
            ]

            if len(matches) != 1:
                # 0 = não encontrou; 2+ = ambíguo; ambos são descartados
                return None
            return matches[0]

        except Exception as exc:
            logger.warning(
                "neo.selecionar_comprovante.erro",
                transacao_id=str(transacao.id),
                erro=str(exc),
            )
            return None

    async def _vincular_comprovante(
        self, transacao: Transacao, comprovante: Comprovante
    ) -> None:
        comprovante.transacao_id = transacao.id
        self._comprovantes_consumidos.add(comprovante.id)
        logger.info(
            "neo.comprovante_associado",
            comprovante_id=str(comprovante.id),
            transacao_id=str(transacao.id),
        )

    async def _tentar_associar_comprovante(self, transacao: Transacao) -> bool:
        """Seleciona e vincula um comprovante candidato, se houver exatamente um."""
        candidato = await self._selecionar_comprovante_candidato(transacao)
        if candidato is None:
            return False
        await self._vincular_comprovante(transacao, candidato)
        return True

    # ── Auto-associação de notas fiscais (Task 6) ─────────────────────────────

    async def _selecionar_nota_candidata(self, transacao: Transacao) -> NotaFiscal | None:
        """Localiza — sem associar — uma nota fiscal candidata à transação.

        Critérios:
          - Mesmo empresa_id
          - Natureza compatível: emitida = crédito; recebida = débito
          - transacao_id IS NULL (ainda não associada)
          - status = 'pendente'
          - |valor - transacao.valor| <= R$ 0,01
          - |data_emissao - transacao.data| <= 7 dias

        Só retorna candidata se houver EXATAMENTE 1 match. Não muta nada —
        quem chama decide se e quando vincular via `_vincular_nota`.
        """
        try:
            data_min = transacao.data - timedelta(days=_DATA_TOLERANCIA_NF)
            data_max = transacao.data + timedelta(days=_DATA_TOLERANCIA_NF)
            valor = Decimal(str(transacao.valor))

            q = select(NotaFiscal).where(
                and_(
                    NotaFiscal.empresa_id == self._empresa_id,
                    NotaFiscal.transacao_id.is_(None),
                    NotaFiscal.deleted_at.is_(None),
                    NotaFiscal.status == "pendente",
                    NotaFiscal.data_emissao >= data_min,
                    NotaFiscal.data_emissao <= data_max,
                )
            )
            if self._notas_consumidas:
                q = q.where(NotaFiscal.id.not_in(self._notas_consumidas))
            q = q.with_for_update(skip_locked=True)
            candidatas = (await self._db.execute(q)).scalars().all()

            matches = [
                nf for nf in candidatas
                if abs(Decimal(str(nf.valor)) - valor) <= _VALOR_TOLERANCIA
                and self._direcao_nota(nf) == transacao.dc
            ]

            if len(matches) != 1:
                return None
            return matches[0]

        except Exception as exc:
            logger.warning(
                "neo.selecionar_nota.erro", transacao_id=str(transacao.id), erro=str(exc)
            )
            return None

    async def _vincular_nota(self, transacao: Transacao, nota: NotaFiscal) -> None:
        nota.transacao_id = transacao.id
        nota.status = "associada"
        self._notas_consumidas.add(nota.id)
        logger.info(
            "neo.nota_associada", nota_id=str(nota.id), transacao_id=str(transacao.id)
        )

    async def _tentar_associar_nota_fiscal(self, transacao: Transacao) -> bool:
        """Seleciona e vincula uma nota fiscal candidata, se houver exatamente uma."""
        candidata = await self._selecionar_nota_candidata(transacao)
        if candidata is None:
            return False
        await self._vincular_nota(transacao, candidata)
        return True

    # ── Queries ───────────────────────────────────────────────────────────────

    async def _carregar_regras(self, agencia_id: UUID | None) -> list[Regra]:
        """Carrega as regras já na ordem em que o matching deve considerá-las.

        A ordem é parte do resultado: nas estratégias `substring` e
        `todas_palavras` a
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

    async def _carregar_pendentes(
        self, agencia_id: UUID | None, mes: str | None = None
    ) -> list[Transacao]:
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
                # Transação apagada não volta para a fila de classificação.
                Transacao.deleted_at.is_(None),
            )
            .order_by(Transacao.data.asc(), Transacao.id.asc())
            .with_for_update(skip_locked=True)
        )
        if agencia_id:
            q = q.where(Transacao.agencia_id == agencia_id)
        if mes:
            inicio, fim = bounds_do_mes_data(mes)
            q = q.where(Transacao.data >= inicio, Transacao.data <= fim)
        return (await self._db.execute(q)).scalars().all()

    async def _carregar_decisoes_sem_regra(
        self, transacoes: list[Transacao]
    ) -> dict[UUID, NeoDecisao]:
        """Decisões 'sem_regra' já abertas para estas transações, indexadas por
        transação.

        Carrega os objetos (e não só os ids) porque quando a transação enfim é
        classificada a decisão aberta precisa ser *encerrada* — ver
        `_registrar_decisao`. O índice parcial
        `uq_neo_sem_regra_transacao` garante no máximo uma por transação.
        """
        if not transacoes:
            return {}
        q = select(NeoDecisao).where(
            NeoDecisao.transacao_id.in_([t.id for t in transacoes]),
            NeoDecisao.resultado == "sem_regra",
        )
        return {
            d.transacao_id: d for d in (await self._db.execute(q)).scalars().all()
        }

    def _direcao_nota(self, nota: NotaFiscal) -> str | None:
        empresa = self._somente_digitos(self._empresa_cnpj)
        emitente = self._somente_digitos(nota.cnpj_emitente)
        destinatario = self._somente_digitos(nota.cnpj_destinatario)
        if empresa and emitente == empresa and destinatario != empresa:
            return "C"
        if empresa and destinatario == empresa and emitente != empresa:
            return "D"
        return None

    @staticmethod
    def _somente_digitos(valor: str | None) -> str:
        return "".join(ch for ch in (valor or "") if ch.isdigit())
