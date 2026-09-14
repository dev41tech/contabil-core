"""Comprovante guarda o hash do arquivo, para o reenvio ser reconhecido.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-14

O QUE ACONTECIA

`ComprovanteService.criar` não comparava nada com o que já existia. Reenviar os
mesmos arquivos criava um segundo registro para cada um — relatado na UNIQUE
MOMENT EVENTOS LTDA.

O dano não é só visual. O NEO associa um comprovante sem vínculo a um débito
apenas quando há EXATAMENTE um candidato de mesmo valor em ±3 dias
(`engine._selecionar_comprovante_candidato`). Com o duplicado são dois, o caso
vira "ambíguo", e a associação automática para em silêncio justamente para os
pagamentos reenviados.

O QUE MUDA NO BANCO

- `comprovantes.arquivo_sha256` VARCHAR(64), nulo — SHA-256 dos BYTES do arquivo
- índice `(empresa_id, arquivo_sha256)`, que sustenta a busca feita a cada envio

POR QUE O ÍNDICE NÃO É UNIQUE

A base já tem os duplicados que motivaram esta migration. Um índice único
falharia ao ser criado, e migration que falha derruba o container inteiro no
`entrypoint.sh`. Apagar os duplicados aqui seria decidir no lugar do contador
qual dos dois registros fica — um deles pode estar associado e o outro não.
Quem barra o duplicado novo é o service; os antigos o contador exclui pela tela.

POR QUE O BACKFILL EXISTE, E POR QUE RODA NO POSTGRES

Sem backfill, só os comprovantes enviados depois do deploy teriam hash — e o
caso relatado é justamente reenviar arquivos que JÁ tinham sido importados.

O hash é calculado dentro do banco (`sha256(decode(...,'base64'))`) em vez de
trazer cada base64 para o Python: o tamanho da tabela em produção não foi medido,
e baixar todos os arquivos no boot poderia segurar a subida do container.

A função temporária devolve NULO para base64 inválido em vez de abortar. Um
arquivo corrompido gravado anos atrás não pode impedir o deploy; ele só fica sem
hash, que é exatamente o estado de hoje.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "comprovantes",
        sa.Column("arquivo_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_comprovante_empresa_arquivo_sha256",
        "comprovantes",
        ["empresa_id", "arquivo_sha256"],
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE FUNCTION pg_temp.sha256_do_base64(conteudo text) RETURNS text AS $$
        BEGIN
            RETURN encode(sha256(decode(conteudo, 'base64')), 'hex');
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql IMMUTABLE
        """
    )
    op.execute(
        """
        UPDATE comprovantes
           SET arquivo_sha256 = pg_temp.sha256_do_base64(arquivo_base64)
         WHERE arquivo_base64 IS NOT NULL
           AND arquivo_sha256 IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_comprovante_empresa_arquivo_sha256", table_name="comprovantes")
    op.drop_column("comprovantes", "arquivo_sha256")
