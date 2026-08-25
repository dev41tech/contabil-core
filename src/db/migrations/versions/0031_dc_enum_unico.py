"""Um único tipo D/C para toda a base.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-25

Eram três tipos com os mesmos dois valores, um por tabela: `dc_enum` (regras),
`dc_transacao_enum` (transações) e `dc_registro_enum` (registros contábeis).

No PostgreSQL enum é tipo NOMINAL: `dc_registro_enum` e `dc_transacao_enum` não
são o mesmo tipo, e comparar os dois é `operator does not exist:
dc_registro_enum = dc_transacao_enum` — a query nem chega a rodar. Em SQLite
enum é texto e a mesma comparação passa, então a suíte deu verde com a aba
Desfeitas quebrada em produção (commit 01b6864). O `cast` para texto que
consertou aquele endpoint é remendo do sintoma; a causa é haver três tipos.

`dc_enum` foi escolhido como o sobrevivente por ser o mais antigo e o de nome
mais genérico — nenhum dos três tem nada de específico da sua tabela.

POR QUE ISTO É SEGURO AQUI

`ALTER COLUMN ... TYPE` reescreve a tabela sob lock exclusivo. Em `transacoes`
e `registros_contabeis` deste sistema isso é da ordem de milhares de linhas por
empresa, medido em segundos — e roda no `alembic upgrade head` do startup, com
o container ainda sem tráfego. Numa base grande a conversa seria outra.

O `USING dc::text::dc_enum` passa pelo texto de propósito: PostgreSQL não
converte enum para enum direto, mesmo quando os valores são idênticos.

O bloco `DO` que garante `dc_enum` existir não é paranoia gratuita: migration
que falha aqui derruba o container inteiro no startup, e o custo de checar é
uma consulta a `pg_type`.

Em SQLite (suíte de testes) o upgrade é no-op: lá enum já é texto com CHECK, os
três "tipos" sempre foram o mesmo, e é justamente por isso que os testes não
enxergam este problema.
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_TABELAS = ("transacoes", "registros_contabeis")
_TIPOS_ABSORVIDOS = ("dc_transacao_enum", "dc_registro_enum")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'dc_enum') THEN
                CREATE TYPE dc_enum AS ENUM ('D', 'C');
            END IF;
        END $$;
        """
    )
    for tabela in _TABELAS:
        op.execute(
            f"ALTER TABLE {tabela} ALTER COLUMN dc TYPE dc_enum "
            f"USING dc::text::dc_enum"
        )
    for tipo in _TIPOS_ABSORVIDOS:
        op.execute(f"DROP TYPE IF EXISTS {tipo}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for tipo in _TIPOS_ABSORVIDOS:
        op.execute(f"CREATE TYPE {tipo} AS ENUM ('D', 'C')")
    for tabela, tipo in zip(_TABELAS, _TIPOS_ABSORVIDOS, strict=True):
        op.execute(
            f"ALTER TABLE {tabela} ALTER COLUMN dc TYPE {tipo} "
            f"USING dc::text::{tipo}"
        )
