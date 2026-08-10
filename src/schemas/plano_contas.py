"""Schemas Pydantic para Plano de Contas."""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# Tipos aceitos pelo sistema contábil brasileiro
TIPOS_VALIDOS = (
    "ativo",
    "passivo",
    "patrimonio_liquido",
    "receita",
    "despesa",
    "custo",
    "resultado",
)

# Código de conta: dígitos separados por ponto — ex: 1, 1.1, 1.1.02, 4.3.1.1
_CODIGO_RE = re.compile(r"^\d+(\.\d+)*$")


class PlanoContaCreate(BaseModel):
    conta_numero: int | None = Field(
        default=None,
        description="Número de conta numérico (ID MrContador). Opcional.",
    )
    codigo: str = Field(
        ...,
        min_length=1,
        max_length=30,
        description="Código contábil (Classificação). Ex: 1, 1.1, 1.1.02",
    )
    descricao: str = Field(..., min_length=2, max_length=300)
    tipo: str = Field(..., description=f"Um de: {TIPOS_VALIDOS}")
    tipo_sa: str = Field(
        default="A",
        description="S = Sintética (agrupamento) | A = Analítica (aceita lançamentos)",
    )
    pai_id: UUID | None = Field(
        default=None,
        description="ID da conta pai. None = conta raiz.",
    )

    @field_validator("tipo_sa")
    @classmethod
    def valida_tipo_sa(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("S", "A"):
            raise ValueError("tipo_sa deve ser 'S' (Sintética) ou 'A' (Analítica).")
        return v

    @field_validator("codigo", mode="before")
    @classmethod
    def normaliza_codigo(cls, v: str) -> str:
        v = v.strip()
        if not _CODIGO_RE.match(v):
            raise ValueError(
                "Código inválido. Use dígitos separados por ponto. Ex: 1, 1.1, 4.2.01"
            )
        return v

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TIPOS_VALIDOS:
            raise ValueError(f"Tipo inválido. Opções: {TIPOS_VALIDOS}")
        return v

    @field_validator("descricao", mode="before")
    @classmethod
    def strip_descricao(cls, v: str) -> str:
        return v.strip()


class PlanoContaUpdate(BaseModel):
    conta_numero: int | None = Field(default=None, description="Número de conta numérico. None mantém o valor atual.")
    codigo: str | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        description="Classificação/código. Imutável após a criação.",
    )
    descricao: str | None = Field(default=None, min_length=2, max_length=300)
    tipo: str | None = None
    tipo_sa: str | None = None

    @field_validator("codigo", mode="before")
    @classmethod
    def normaliza_codigo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not _CODIGO_RE.match(v):
            raise ValueError(
                "Código inválido. Use dígitos separados por ponto. Ex: 1, 1.1, 4.2.01"
            )
        return v

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in TIPOS_VALIDOS:
            raise ValueError(f"Tipo inválido. Opções: {TIPOS_VALIDOS}")
        return v

    @field_validator("tipo_sa")
    @classmethod
    def valida_tipo_sa(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in ("S", "A"):
            raise ValueError("tipo_sa deve ser 'S' ou 'A'.")
        return v

    @field_validator("descricao", mode="before")
    @classmethod
    def strip_descricao(cls, v: str | None) -> str | None:
        return v.strip() if v else v


class PlanoContaResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    conta_numero: int | None = None  # ID numérico MrContador
    codigo: str
    descricao: str
    tipo: str
    tipo_sa: str = "A"
    pai_id: UUID | None
    nivel: int  # propriedade computada

    model_config = {"from_attributes": True}


class PlanoContaNode(PlanoContaResponse):
    """Conta com seus filhos diretos para exibição em árvore."""
    filhos: list[PlanoContaNode] = []

    model_config = {"from_attributes": True}


# Necessário para referência recursiva
PlanoContaNode.model_rebuild()


class PlanoContaListResponse(BaseModel):
    items: list[PlanoContaResponse]
    total: int


class PlanoContaTreeResponse(BaseModel):
    """Resposta em árvore — apenas contas raiz com seus filhos aninhados."""
    tree: list[PlanoContaNode]
    total: int


class PlanoContaExclusaoLoteRequest(BaseModel):
    """Exclui contas selecionadas (ids) ou todo o plano de contas (todas=True)."""
    ids: list[UUID] = Field(default_factory=list)
    todas: bool = Field(
        default=False,
        description="Se True, ignora 'ids' e tenta remover todas as contas ativas da empresa.",
    )

    @model_validator(mode="after")
    def valida_selecao(self) -> "PlanoContaExclusaoLoteRequest":
        if not self.todas and not self.ids:
            raise ValueError("Informe 'ids' ou defina 'todas' como True.")
        return self


class PlanoContaExclusaoBloqueada(BaseModel):
    id: UUID
    codigo: str
    erro: str


class PlanoContaExclusaoLoteResultado(BaseModel):
    removidas: int
    bloqueadas: list[PlanoContaExclusaoBloqueada]
