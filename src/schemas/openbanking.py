"""Schemas Pydantic para Open Banking."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ConnectTokenResponse(BaseModel):
    access_token: str
    provedor: str       # "pluggy" | "mock"
    mock_mode: bool     # True = sem credenciais Pluggy, usar fluxo simplificado


class SalvarConexaoRequest(BaseModel):
    item_id: str = Field(..., min_length=1, description="ID do item retornado pelo widget")
    # Usado apenas no mock mode
    instituicao_nome: str | None = None


class SincronizarRequest(BaseModel):
    dias: int = Field(
        default=90,
        ge=1,
        le=365,
        description="Quantos dias retroativos sincronizar",
    )


class ConexaoResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    agencia_id: UUID | None
    provedor: str
    item_id: str
    instituicao_nome: str
    instituicao_codigo: str | None
    banco_sigla: str
    agencia_numero: str | None
    conta_numero: str | None
    status: str
    last_sync_at: datetime | None
    next_sync_at: datetime | None
    total_transacoes_sync: int
    erro_msg: str | None

    model_config = {"from_attributes": True}


class ConexaoListResponse(BaseModel):
    items: list[ConexaoResponse]
    total: int


class SincronizarResponse(BaseModel):
    conexao_id: UUID
    importadas: int
    duplicadas: int
    erros: int
    periodo_inicio: str
    periodo_fim: str
    status: str
