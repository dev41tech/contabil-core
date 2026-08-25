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


def test_dc_e_um_tipo_so_em_todas_as_tabelas():
    """Um enum D/C por tabela foi o que quebrou a aba Desfeitas em produção.

    Eram três tipos com os mesmos dois valores. No PostgreSQL enum é tipo
    nominal, então comparar `dc_registro_enum` com `dc_transacao_enum` é
    "operator does not exist" e a query nem roda; em SQLite enum é texto e a
    mesma comparação passa — a suíte ficava verde com o endpoint quebrado.

    Este teste é a única barreira que a suíte consegue oferecer contra isso
    voltar: ele olha o TIPO declarado nos models, não o banco. Que o banco de
    produção esteja de fato com um tipo só depende da migration 0031, e SQLite
    não tem como verificar.
    """
    from src.db.models import Regra, RegistroContabil, Transacao

    nomes = {
        modelo.__table__.c.dc.type.name
        for modelo in (Regra, RegistroContabil, Transacao)
    }
    assert nomes == {"dc_enum"}, (
        f"D/C voltou a ter mais de um tipo: {sorted(nomes)}. Reusar `DC_ENUM` "
        f"de src.db.models é o que impede a comparação entre tabelas de "
        f"quebrar no Postgres."
    )


def test_comparar_dc_entre_tabelas_nao_precisa_mais_de_cast():
    """Com um tipo só, a comparação volta a ser direta.

    O `cast` para texto que a aba Desfeitas carregava era remendo do sintoma —
    saiu junto com a causa. Se ele reaparecer, é sinal de que alguém criou um
    enum novo por tabela outra vez.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from src.db.models import RegistroContabil, Transacao

    q = select(RegistroContabil.id).where(RegistroContabil.dc == Transacao.dc)
    sql = str(q.compile(dialect=postgresql.dialect()))

    assert "CAST" not in sql.upper()
