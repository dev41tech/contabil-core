"""Schemas Pydantic para o NEO (motor de matching)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.schemas.regras import RegraCreate, RegraResponse
from src.schemas.types import Competencia


class NeoProcessarRequest(BaseModel):
    agencia_id: UUID | None = None  # None = processar todas as agências da empresa
    mes: Competencia | None = Field(
        default=None,
        description="Filtra transações do mês (AAAA-MM). None = processa todas as pendentes.",
    )


class NeoResultado(BaseModel):
    empresa_id: UUID
    total_pendentes: int
    associadas: int
    sem_regra: int
    erros: int
    # Auto-associações (tasks 5 & 6)
    comprovantes_associados: int = 0
    notas_associadas: int = 0
    # Classificadas sem regra, só pelo cadastro de contrapartes (itens 1+2 do
    # PDF de feedback dos contadores) — subconjunto de `associadas`.
    classificadas_por_contraparte: int = 0
    # Barradas antes de qualquer classificação porque o valor gravado não é
    # confiável — subconjunto de `sem_regra`, não uma quarta categoria: elas
    # continuam pendentes e voltam a ser tentadas quando o extrato for
    # corrigido.
    bloqueadas_valor_suspeito: int = 0
    processado_em: datetime


class NeoDecisaoResponse(BaseModel):
    id: UUID
    transacao_id: UUID
    transacao_descricao: str | None = None
    # Campos extras para popular os modais de ação manual
    transacao_valor: Decimal | None = None
    transacao_dc: str | None = None
    # A data do lançamento vem junto porque a fila de classificação mostra
    # "Data | Histórico | Valor": sem ela a tela teria de buscar transação por
    # transação, um N+1 para exibir uma coluna.
    transacao_data: date | None = None
    # Lançamento contábil vigente da transação, quando ela está classificada.
    # É o que a tela precisa para oferecer "Desfazer" — o índice único parcial
    # garante no máximo um par ativo por transação, então não há ambiguidade.
    lancamento_id: UUID | None = None
    # Histórico CONTÁBIL do lançamento vigente — o texto que foi para o razão,
    # que é o que o contador escreveu ao classificar (ou o que a regra
    # produziu). Diferente de `transacao_descricao`, que é a linha crua do
    # banco e nunca muda.
    lancamento_historico: str | None = None
    agencia_id: UUID | None = None
    regra_id: UUID | None
    regra_descricao: str | None = None
    conta_id: UUID | None = None
    conta_codigo: str | None = None
    conta_descricao: str | None = None
    resultado: str       # associada | sem_regra | erro
    estrategia: str | None
    motivo: str | None
    processado_em: datetime

    model_config = {"from_attributes": True}


class NeoDecisaoListResponse(BaseModel):
    items: list[NeoDecisaoResponse]
    total: int
    page: int
    page_size: int


class NeoDivergenciaPorContaResponse(BaseModel):
    conta_regra_id: UUID
    conta_regra_codigo: str
    conta_regra_descricao: str
    conta_contraparte_id: UUID
    conta_contraparte_codigo: str
    conta_contraparte_descricao: str
    quantidade: int
    valor_total: Decimal


class NeoDivergenciaAmostraResponse(BaseModel):
    decisao_id: UUID
    transacao_id: UUID
    historico: str
    valor: Decimal
    origem_evidencia: str
    contraparte_id: UUID
    conta_regra_id: UUID
    conta_regra_codigo: str
    conta_regra_descricao: str
    conta_contraparte_id: UUID
    conta_contraparte_codigo: str
    conta_contraparte_descricao: str


class NeoDivergenciasResponse(BaseModel):
    total_avaliadas: int
    total_divergentes: int
    percentual_divergentes: float
    valor_total_divergente: Decimal
    por_conta: list[NeoDivergenciaPorContaResponse]
    amostra: list[NeoDivergenciaAmostraResponse]


class NeoPendenciaGrupoResponse(BaseModel):
    padrao: str
    rotulo: str
    dc: str
    quantidade: int
    valor_total: Decimal
    data_inicio: datetime
    data_fim: datetime
    agencia_ids: list[UUID]
    amostras: list[str]
    transacao_ids: list[UUID]


class NeoPendenciaResponse(BaseModel):
    """Uma linha da fila de classificação.

    Nasce da TRANSAÇÃO, não da decisão: transação recém-importada que o motor
    ainda não olhou não tem decisão nenhuma, e uma fila que só mostrasse
    decisões esconderia justamente o que falta processar.
    """

    transacao_id: UUID
    # Nulo enquanto o NEO nunca rodou sobre esta transação.
    decisao_id: UUID | None = None
    data: date
    historico: str
    valor: Decimal
    dc: str
    agencia_id: UUID
    # O que o motor achou e por que não classificou — inclui o aviso de valor
    # não confiável e a recusa por contraparte ambígua.
    motivo: str | None = None
    # "valor_suspeito" marca a transação que o motor se recusa a contabilizar.
    estrategia: str | None = None
    # Preenchido quando um humano desfez a classificação automática: enquanto
    # estiver aqui, o motor não tenta de novo sozinho.
    auto_recusado_em: datetime | None = None


class NeoPendenciaListResponse(BaseModel):
    items: list[NeoPendenciaResponse]
    total: int
    page: int
    page_size: int


class NeoPendenciasAgrupadasResponse(BaseModel):
    grupos: list[NeoPendenciaGrupoResponse]
    total_pendentes: int
    total_agrupadas: int
    total_grupos: int
    # True quando havia mais pendências do que o teto de varredura: os grupos
    # descrevem só a fatia mais antiga. A tela precisa dizer isso ao contador
    # em vez de deixá-lo achar que está vendo tudo.
    parcial: bool = False


class NeoAssociarManualRequest(BaseModel):
    conta_id: UUID
    descricao: str = Field(..., min_length=2, max_length=500)


class NeoClassificarLoteRequest(BaseModel):
    transacao_ids: list[UUID] = Field(..., min_length=1, max_length=200)
    conta_id: UUID
    descricao: str = Field(..., min_length=2, max_length=500)

    @field_validator("transacao_ids")
    @classmethod
    def remover_ids_repetidos(cls, ids: list[UUID]) -> list[UUID]:
        # Repetições podem surgir ao mesclar grupos na tela. Removê-las aqui
        # impede uma segunda tentativa de contabilizar a mesma transação.
        return list(dict.fromkeys(ids))


class NeoClassificarLoteBloqueio(BaseModel):
    transacao_id: UUID
    motivo: str


class NeoClassificarLoteResponse(BaseModel):
    classificadas: int
    ignoradas: int
    ids_ignorados: list[UUID]
    # `ignoradas` é fotografia velha (id sumiu, mudou de empresa, já foi
    # processado) e não pede ação. `bloqueadas` é o contrário: a linha existe,
    # está pendente, e foi recusada por um motivo que o contador precisa ler —
    # por isso vem com texto, e não só com o id.
    bloqueadas: int = 0
    bloqueios: list[NeoClassificarLoteBloqueio] = Field(default_factory=list)


class NeoSimularRegraRequest(BaseModel):
    historico: str = Field(..., min_length=2, max_length=500)
    dc: str
    agencia_id: UUID
    conta_id: UUID

    @field_validator("historico", mode="before")
    @classmethod
    def limpar_historico(cls, valor: str) -> str:
        return valor.strip()

    @field_validator("dc")
    @classmethod
    def validar_dc(cls, valor: str) -> str:
        valor = valor.strip().upper()
        if valor not in ("D", "C"):
            raise ValueError("dc deve ser 'D' (Débito) ou 'C' (Crédito).")
        return valor


class NeoSimulacaoResumo(BaseModel):
    quantidade: int
    amostras: list[str] = Field(default_factory=list)


class NeoSimulacaoQuantidade(BaseModel):
    quantidade: int


class NeoConflitoAmostra(BaseModel):
    transacao_id: UUID
    historico: str
    conta_id: UUID


class NeoSimulacaoConflitos(BaseModel):
    quantidade: int
    amostras: list[NeoConflitoAmostra]


class NeoSimularRegraResponse(BaseModel):
    pendencias_atingidas: NeoSimulacaoResumo
    ja_contabilizadas_atingidas: NeoSimulacaoQuantidade
    conflitos: NeoSimulacaoConflitos


class NeoCriarRegraEAplicarRequest(RegraCreate):
    mes: Competencia | None = None


class NeoCriarRegraEAplicarResponse(BaseModel):
    regra: RegraResponse
    resultado: NeoResultado


class NeoCancelarLancamentoRequest(BaseModel):
    # Motivo é exigido pela API, não só pelo formulário: desfazer sem motivo
    # registrado transforma a trilha de auditoria numa lista de carimbos, e
    # qualquer chamada direta contornaria uma validação que morasse só na tela.
    motivo: str = Field(..., min_length=3, max_length=300)

    @field_validator("motivo", mode="before")
    @classmethod
    def limpar_motivo(cls, valor: str) -> str:
        return valor.strip() if isinstance(valor, str) else valor


class NeoCancelarLancamentoResponse(BaseModel):
    transacao_id: UUID
    partidas_canceladas: int
    notas_desvinculadas: int
    comprovantes_desvinculados: int


class NeoDesfeitaResponse(BaseModel):
    lancamento_id: UUID
    transacao_id: UUID
    transacao_data: date | None = None
    transacao_descricao: str | None = None
    valor: Decimal
    dc: str
    conta_descricao: str
    cancelado_em: datetime
    cancelado_por_nome: str | None = None
    motivo_cancelamento: str | None = None
    # Preenchidos quando o lançamento veio de um upload. `lote_cancelado` diz se
    # o upload INTEIRO foi desfeito — é o que a tela usa para agrupar os itens
    # sob o arquivo em vez de espalhá-los numa lista plana.
    importacao_id: UUID | None = None
    importacao_arquivo: str | None = None
    lote_cancelado: bool = False
    # Estado atual da transação e a decisão contra a qual associar. `None` em
    # `decisao_atual_id` significa que não há o que reclassificar agora — ou
    # porque já foi, ou porque o motor ainda não registrou pendência.
    transacao_status: str | None = None
    decisao_atual_id: UUID | None = None
    # O motor não reclassifica esta transação enquanto isto for True — é a
    # prioridade do manual sobre o automático. A tela explica e oferece liberar.
    aguardando_decisao_manual: bool = False


class NeoDesfeitaListResponse(BaseModel):
    items: list[NeoDesfeitaResponse]
    total: int
    page: int
    page_size: int
