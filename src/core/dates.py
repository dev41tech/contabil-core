"""Utilitários de data compartilhados entre domínios."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def bounds_do_mes(mes: str) -> tuple[datetime, datetime]:
    """Converte 'AAAA-MM' no intervalo [primeiro instante, último instante] do mês."""
    ano, mes_num = int(mes[:4]), int(mes[5:7])
    inicio = datetime(ano, mes_num, 1, tzinfo=UTC)
    fim_exclusivo = (
        datetime(ano + 1, 1, 1, tzinfo=UTC)
        if mes_num == 12
        else datetime(ano, mes_num + 1, 1, tzinfo=UTC)
    )
    return inicio, fim_exclusivo - timedelta(microseconds=1)
