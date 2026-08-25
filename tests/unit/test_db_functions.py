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


def test_desfeitas_compara_dc_com_cast_explicito():
    """`RegistroContabil.dc` e `Transacao.dc` são enums DIFERENTES no Postgres.

    Comparar os dois sem cast é erro de tipo lá — "operator does not exist:
    dc_registro_enum = dc_transacao_enum" — mas passa em SQLite, onde enum é
    texto. Foi assim que a aba Desfeitas foi para produção quebrada com a suíte
    verde.

    O teste compila a consulta no dialeto PostgreSQL e exige o CAST, porque é a
    única forma de pegar isso sem um Postgres de verdade na suíte.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy import String, cast, select

    from src.db.models import RegistroContabil, Transacao

    q = select(RegistroContabil.id).where(
        cast(RegistroContabil.dc, String) == cast(Transacao.dc, String)
    )
    sql = str(q.compile(dialect=postgresql.dialect()))

    assert "CAST" in sql.upper(), (
        "a comparação entre os dois enums precisa de cast explícito no Postgres"
    )


def test_comparar_os_dois_enums_sem_cast_seria_invalido_no_postgres():
    """Guarda o motivo: os tipos SÃO diferentes, não é preciosismo."""
    from src.db.models import RegistroContabil, Transacao

    assert RegistroContabil.__table__.c.dc.type.name == "dc_registro_enum"
    assert Transacao.__table__.c.dc.type.name == "dc_transacao_enum"
    assert (
        RegistroContabil.__table__.c.dc.type.name
        != Transacao.__table__.c.dc.type.name
    )
