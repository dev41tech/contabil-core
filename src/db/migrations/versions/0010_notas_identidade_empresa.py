"""Escopa a identidade de notas fiscais por empresa.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-07
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notas_fiscais", sa.Column("dedup_key", sa.String(64), nullable=True))
    bind = op.get_bind()
    notas = bind.execute(
        sa.text(
            """
            SELECT id, empresa_id, tipo, numero, serie, cnpj_emitente, valor,
                   data_emissao, chave_acesso
              FROM notas_fiscais
            """
        )
    ).mappings()
    vistos: set[tuple[object, str]] = set()
    chaves_vistas: set[tuple[object, str]] = set()
    for nota in notas:
        digits = re.sub(r"\D", "", nota["chave_acesso"] or "")
        chave = digits if len(digits) == 44 else None
        if nota["tipo"] == "nfe" and chave:
            origem = f"nfe|{chave}"
        else:
            data = nota["data_emissao"]
            if data.tzinfo is None:
                data = data.replace(tzinfo=UTC)
            else:
                data = data.astimezone(UTC)
            origem = "|".join(
                (
                    "nfse" if nota["tipo"] == "nfse" else "nfe-sem-chave",
                    re.sub(r"\D", "", nota["cnpj_emitente"] or ""),
                    nota["numero"].strip(),
                    (nota["serie"] or "").strip(),
                    str(Decimal(str(nota["valor"])).quantize(Decimal("0.01"))),
                    data.isoformat(),
                )
            )
        dedup = hashlib.sha256(origem.encode("utf-8")).hexdigest()
        identidade = (nota["empresa_id"], dedup)
        if identidade in vistos:
            dedup = hashlib.sha256(
                f"duplicata-legada|{nota['id']}|{dedup}".encode("utf-8")
            ).hexdigest()
        vistos.add((nota["empresa_id"], dedup))
        if chave:
            identidade_chave = (nota["empresa_id"], chave)
            if identidade_chave in chaves_vistas:
                chave = None
            else:
                chaves_vistas.add(identidade_chave)
        bind.execute(
            sa.text(
                """
                UPDATE notas_fiscais
                   SET dedup_key = :dedup, chave_acesso = :chave
                 WHERE id = :id
                """
            ),
            {"dedup": dedup, "chave": chave, "id": nota["id"]},
        )
    op.alter_column("notas_fiscais", "dedup_key", nullable=False)
    op.drop_constraint("notas_fiscais_chave_acesso_key", "notas_fiscais", type_="unique")
    op.alter_column(
        "notas_fiscais",
        "chave_acesso",
        existing_type=sa.String(60),
        type_=sa.String(44),
        existing_nullable=True,
    )
    op.create_unique_constraint(
        "uq_nota_empresa_chave", "notas_fiscais", ["empresa_id", "chave_acesso"]
    )
    op.create_unique_constraint(
        "uq_nota_empresa_dedup", "notas_fiscais", ["empresa_id", "dedup_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_nota_empresa_dedup", "notas_fiscais", type_="unique")
    op.drop_constraint("uq_nota_empresa_chave", "notas_fiscais", type_="unique")
    op.alter_column(
        "notas_fiscais",
        "chave_acesso",
        existing_type=sa.String(44),
        type_=sa.String(60),
        existing_nullable=True,
    )
    op.create_unique_constraint(
        "notas_fiscais_chave_acesso_key", "notas_fiscais", ["chave_acesso"]
    )
    op.drop_column("notas_fiscais", "dedup_key")
