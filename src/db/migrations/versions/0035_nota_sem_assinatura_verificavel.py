"""Nota fiscal pode entrar sem assinatura verificável, com o motivo registrado.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-01

A DECISÃO

O downloader de DF-e que o escritório usa entrega os XML **reempacotados e
alterados depois de assinados**: a nota autorizada vem dentro de um `<NFeLog>`
junto dos eventos dela, e o conteúdo não bate mais com a própria assinatura —
num dos exemplos o e-mail do destinatário chega mascarado (`j*m@g*l*.com`).

Conferido na mão nos três arquivos reais: a canonicalização do `SignedInfo` não
fecha e o digest da referência não bate. Não é defeito do parser; são exportações
processadas, e a assinatura delas nunca vai conferir.

Recusá-las significava o escritório não importar nota fiscal nenhuma. Em
01/09/2026 o Nathan decidiu aceitar, com a procedência registrada.

O QUE SUSTENTA O DOCUMENTO NO LUGAR DA ASSINATURA

O protocolo de autorização da SEFAZ, que continua OBRIGATÓRIO: NF-e sem
`protNFe` segue recusada pelo parser. É o que impede este afrouxamento de virar
"aceita qualquer XML" — o documento precisa ter sido autorizado.

O QUE MUDA NO BANCO

`origem_nota_enum` ganha `xml_nao_verificado`, entre `xml_assinado` e `ocr` na
escala de confiança, e `notas_fiscais.assinatura_motivo` guarda por que a
assinatura não fechou. Sem o motivo, a origem diria que algo falhou sem dizer o
quê — e a decisão deixaria de ser auditável.

SEGURANÇA DO ALTER

Acrescentar valor a um enum e adicionar coluna nula não reescrevem linha nem
reclassificam nota existente: tudo que está lá continua `xml_assinado`, porque
foi importado quando a verificação era obrigatória.

`ALTER TYPE ... ADD VALUE` não roda dentro de transação em Postgres antigo; o
`autocommit_block` existe por isso.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE origem_nota_enum ADD VALUE IF NOT EXISTS 'xml_nao_verificado'")
    op.add_column(
        "notas_fiscais",
        sa.Column("assinatura_motivo", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    """Remove a coluna; o valor do enum FICA.

    Postgres não sabe remover valor de enum, e reescrever o tipo exigiria
    reclassificar as notas que já entraram como `xml_nao_verificado` — decisão
    de contador, não de migration. O valor órfão não faz mal: nenhum código o
    grava depois do downgrade.
    """
    op.drop_column("notas_fiscais", "assinatura_motivo")
