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


# Mapa usado pelas duas implementações de `sem_acento` — mantido aqui (e não
# duplicado por dialeto) para que Postgres e SQLite dobrem exatamente o mesmo
# conjunto de caracteres. Inclui maiúsculas acentuadas mapeadas direto para a
# minúscula sem acento porque o `lower()` do SQLite é ASCII-only: sem isso,
# "LIQUIDAÇÃO" viraria "liquidaÇÃo" nos testes e o mesmo termo casaria em
# produção e não casaria na suíte.
_ACENTOS = "áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ"
_SEM_ACENTO = "aaaaaeeeeiiiiooooouuuucnaaaaaeeeeiiiiooooouuuucn"


class sem_acento(FunctionElement):
    """Devolve a coluna em minúsculas e sem acento, para busca textual.

    Existe porque o contador digita "LIQUIDACAO" e o extrato do banco traz
    "LIQUIDAÇÃO" (ou o contrário): com `ilike` puro a busca do NEO não achava
    nada e parecia quebrada.

    Uso: `sem_acento(Transacao.historico).like(f"%{termo_normalizado}%")`, onde
    `termo_normalizado` já passou por `src.core.texto.normalizar_para_match`.
    """

    name = "sem_acento"
    type = String()
    inherit_cache = True


@compiles(sem_acento)
def _sem_acento_padrao(element, compiler, **kw):
    """PostgreSQL e demais dialetos com `translate`."""
    (coluna,) = element.clauses
    return compiler.process(
        func.lower(func.translate(coluna, _ACENTOS, _SEM_ACENTO)), **kw
    )


@compiles(sem_acento, "sqlite")
def _sem_acento_sqlite(element, compiler, **kw):
    """SQLite não tem `translate` — vira uma cadeia de `replace` equivalente."""
    (coluna,) = element.clauses
    expr = coluna
    for origem, destino in zip(_ACENTOS, _SEM_ACENTO, strict=True):
        expr = func.replace(expr, origem, destino)
    return compiler.process(func.lower(expr), **kw)
