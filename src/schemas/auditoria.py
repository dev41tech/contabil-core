"""Schemas da leitura administrativa do trilho de auditoria."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class AuditoriaItemResponse(BaseModel):
    id: UUID
    usuario_id: UUID | None
    usuario_nome: str | None
    usuario_email: str | None
    quando: datetime
    acao: str
    entidade: str
    entidade_id: str | None
    dados_antes: dict[str, Any] | None
    dados_depois: dict[str, Any] | None


class AuditoriaListResponse(BaseModel):
    items: list[AuditoriaItemResponse]
    total: int
    page: int
    page_size: int
