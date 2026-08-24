"""Schemas Pydantic para Registro Contábil."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RegistroContabilResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    transacao_id: UUID | None
    lancamento_id: UUID
    conta_id: UUID
    agencia_id: UUID
    descricao: str
    historico: str
    historico_extrato: str
    dc: str
    tipo_regra: str
    valor: Decimal
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
    formato: str = Field(default="xlsx", description="csv, xlsx ou txt")
    tipo: str = Field(
        default="lancamentos",
        description=(
            "lancamentos | lancamentos_importacao | extrato | nfe_entrada | "
            "nfe_saida | nfse_tomado | nfse_prestado | conferencia"
        ),
    )
    data_de: datetime | None = None
    data_ate: datetime | None = None
    mes: str | None = Field(
        default=None,
        description=(
            "Atalho para filtrar um mês específico, formato AAAA-MM. Quando "
            "informado, tem precedência sobre data_de/data_ate."
        ),
    )

    # Filtros usados apenas pelo tipo `extrato`, para o arquivo sair com as
    # mesmas linhas que a tela mostra. Ignorados nos demais tipos.
    agencia_id: UUID | None = None
    status: str | None = Field(
        default=None, description="extrato: pendente | processada | erro"
    )
    historico: str | None = Field(
        default=None, description="extrato: busca parcial no histórico do banco"
    )
    dc: str | None = Field(default=None, pattern="^[DC]$", description="extrato: D ou C")
    # `Transacao.valor` é sempre positivo — o sinal mora em `dc`. A faixa é sobre
    # o módulo do lançamento, igual ao filtro da tela.
    valor_min: Decimal | None = Field(default=None, ge=0)
    valor_max: Decimal | None = Field(default=None, ge=0)

    @field_validator("mes")
    @classmethod
    def valida_mes(cls, v: str | None) -> str | None:
        if v is None:
            return None
        m = re.fullmatch(r"(\d{4})-(\d{2})", v)
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise ValueError("mes deve estar no formato AAAA-MM, com mês entre 01 e 12.")
        return v


class ExportJobResponse(BaseModel):
    job_id: UUID
    status: str
    formato: str
    total_registros: int | None
    download_url: str | None = None
    erro_msg: str | None = None

    model_config = {"from_attributes": True}
