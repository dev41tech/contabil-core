"""Expressões SQL que precisam de tradução por dialeto.

O banco de produção é PostgreSQL, mas a suíte roda em SQLite (sem Docker). Quando
uma query usa função exclusiva de um dialeto, o código deixa de ser testável — foi
o que manteve o domínio `stats` sem nenhum teste: `to_char` não existe no SQLite,
então qualquer chamada ao endpoint estourava `OperationalError`.
"""

from __future__ import annotations

from sqlalchemy import String, func
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement


class mes_ano(FunctionElement):
    """Formata uma coluna de data/hora como `'YYYY-MM'`, para agrupar por mês.

    Uso: `select(mes_ano(Transacao.data).label("mes"), func.count())...`
    """

    name = "mes_ano"
    type = String()
    inherit_cache = True


@compiles(mes_ano)
def _mes_ano_padrao(element, compiler, **kw):
    """PostgreSQL e demais dialetos com `to_char`."""
    (coluna,) = element.clauses
    return compiler.process(func.to_char(coluna, "YYYY-MM"), **kw)


@compiles(mes_ano, "sqlite")
def _mes_ano_sqlite(element, compiler, **kw):
    (coluna,) = element.clauses
    return compiler.process(func.strftime("%Y-%m", coluna), **kw)
