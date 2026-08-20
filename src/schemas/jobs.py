"""Contratos da API de jobs persistentes."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

JobTipo = Literal["neo_processar", "extrato_importar"]
JobStatus = Literal[
    "na_fila", "processando", "concluido", "concluido_com_alertas", "falhou"
]


class JobResponse(BaseModel):
    id: UUID
    empresa_id: UUID | None
    tipo: JobTipo
    status: JobStatus
    total: int | None
    processados: int | None
    resultado: dict[str, Any] | None
    erro: str | None
    criado_por: UUID
    created_at: datetime
    iniciado_em: datetime | None
    concluido_em: datetime | None
    heartbeat_em: datetime | None

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int
    page: int
    page_size: int
