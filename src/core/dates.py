"""Utilitários de data compartilhados entre domínios."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def bounds_do_mes(mes: str) -> tuple[datetime, datetime]:
    """Converte 'AAAA-MM' no intervalo [primeiro instante, último instante] do mês.

    Para colunas que guardam um INSTANTE (`created_at`, `data_lancamento`). Data de
    lançamento bancário é data de calendário — use `bounds_do_mes_data`.
    """
    ano, mes_num = int(mes[:4]), int(mes[5:7])
    inicio = datetime(ano, mes_num, 1, tzinfo=UTC)
    fim_exclusivo = (
        datetime(ano + 1, 1, 1, tzinfo=UTC)
        if mes_num == 12
        else datetime(ano, mes_num + 1, 1, tzinfo=UTC)
    )
    return inicio, fim_exclusivo - timedelta(microseconds=1)


def bounds_do_mes_data(mes: str) -> tuple[date, date]:
    """Converte 'AAAA-MM' no intervalo [primeiro dia, último dia] do mês.

    Para colunas `Date`, onde o último dia é o dia inteiro — não "o último dia às
    00:00", que é o que fazia o filtro por período descartar os lançamentos do
    último dia do intervalo.
    """
    ano, mes_num = int(mes[:4]), int(mes[5:7])
    primeiro = date(ano, mes_num, 1)
    proximo = date(ano + 1, 1, 1) if mes_num == 12 else date(ano, mes_num + 1, 1)
    return primeiro, proximo - timedelta(days=1)
