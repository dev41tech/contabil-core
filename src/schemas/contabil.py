"""Schemas Pydantic para Registro Contábil."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class RegistroContabilResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    transacao_id: UUID | None
    conta_id: UUID
    agencia_id: UUID
    descricao: str
    historico: str
    historico_extrato: str
    dc: str
    tipo_regra: str
    valor: float
    data_lancamento: datetime
    # Expandidos
    conta_codigo: str | None = None
    conta_descricao: str | None = None
    agencia_descricao: str | None = None

    model_config = {"from_attributes": True}


class RegistroContabilListResponse(BaseModel):
    items: list[RegistroContabilResponse]
    total: int
    page: int
    page_size: int


class ExportJobCreate(BaseModel):
    formato: str = Field(default="xlsx", description="csv ou xlsx")
    tipo: str = Field(
        default="lancamentos",
        description=(
            "lancamentos | nfe_entrada | nfe_saida | "
            "nfse_tomado | nfse_prestado | conferencia"
        ),
    )
    data_de: datetime | None = None
    data_ate: datetime | None = None


class ExportJobResponse(BaseModel):
    job_id: UUID
    status: str
    formato: str
    total_registros: int | None
    download_url: str | None = None
    erro_msg: str | None = None

    model_config = {"from_attributes": True}
