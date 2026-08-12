"""Adiciona origem (xml_assinado | ocr) às notas fiscais.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-12

Nota fiscal importada por XML passa por verificação de assinatura digital
(`_validar_assinatura`, certificado X.509) — garantia criptográfica de
autenticidade. Nota importada por PDF/imagem (novo caminho de OCR/Vision,
`src/domain/notas/visual_parser.py`) não tem essa garantia: é só extração
visual, falsificável. Esta coluna distingue as duas origens para quem for
construir a distinção visual/de fluxo no frontend depois.

Backfill: toda nota existente veio do caminho XML (o único que existia até
aqui), então 'xml_assinado' é o default seguro para linhas já cadastradas.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

origem_nota_enum = postgresql.ENUM("xml_assinado", "ocr", name="origem_nota_enum")


def upgrade() -> None:
    origem_nota_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "notas_fiscais",
        sa.Column(
            "origem",
            origem_nota_enum,
            nullable=False,
            server_default="xml_assinado",
        ),
    )
    op.alter_column("notas_fiscais", "origem", server_default=None)


def downgrade() -> None:
    op.drop_column("notas_fiscais", "origem")
    origem_nota_enum.drop(op.get_bind(), checkfirst=True)
