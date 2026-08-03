"""Testes unitários — expressões SQL traduzidas por dialeto.

A suíte roda em SQLite e a produção é PostgreSQL, então o caminho do Postgres nunca
é exercitado pelos testes de integração. Aqui a garantia é feita compilando a
expressão contra cada dialeto e conferindo o SQL gerado.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from src.db.functions import mes_ano
from src.db.models import Transacao


def _compilar(dialeto) -> str:
    return str(
        select(mes_ano(Transacao.data).label("mes")).compile(dialect=dialeto)
    )


def test_postgres_usa_to_char():
    """O comportamento em produção não pode mudar: continua `to_char(coluna, 'YYYY-MM')`."""
    sql = _compilar(postgresql.dialect())
    assert "to_char" in sql
    assert "strftime" not in sql


def test_sqlite_usa_strftime():
    sql = _compilar(sqlite.dialect())
    assert "strftime" in sql
    assert "to_char" not in sql


def test_expressao_pode_ser_agrupada():
    """Precisa funcionar como chave de GROUP BY — é o uso real no domínio stats."""
    q = select(mes_ano(Transacao.data).label("mes")).group_by("mes")
    sql = str(q.compile(dialect=postgresql.dialect()))
    assert "GROUP BY" in sql
