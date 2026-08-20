"""Conversão de competência mensal em limites temporais portáveis."""

from __future__ import annotations

from datetime import UTC, datetime

from src.schemas.types import normalizar_competencia


def competencia_atual() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def limites_competencia(mes: str) -> tuple[datetime, datetime]:
    """Retorna o intervalo fechado-aberto do mês, sem função SQL de dialeto."""
    competencia = normalizar_competencia(mes)
    ano, numero_mes = (int(parte) for parte in competencia.split("-"))
    inicio = datetime(ano, numero_mes, 1, tzinfo=UTC)
    if numero_mes == 12:
        fim = datetime(ano + 1, 1, 1, tzinfo=UTC)
    else:
        fim = datetime(ano, numero_mes + 1, 1, tzinfo=UTC)
    return inicio, fim
