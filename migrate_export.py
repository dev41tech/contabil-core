"""Exporta empresas e planos de contas de um tenant para SQL de migração."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

sys.path.insert(0, ".")

DEFAULT_OUTPUT_FILE = Path("migrate_prod.sql")


def to_sql_val(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta empresas e planos de contas de um único tenant."
    )
    parser.add_argument(
        "--tenant-origem",
        type=UUID,
        required=True,
        help="UUID do tenant no banco local",
    )
    parser.add_argument(
        "--tenant-destino",
        type=UUID,
        required=True,
        help="UUID do tenant que receberá os dados",
    )
    parser.add_argument(
        "--arquivo",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help=f"arquivo SQL de saída (padrão: {DEFAULT_OUTPUT_FILE})",
    )
    return parser.parse_args()


def validar_escopo(
    empresas: Sequence[Mapping[str, object]],
    planos: Sequence[Mapping[str, object]],
    tenant_origem: UUID,
) -> None:
    """Impede a geração se qualquer linha carregada escapar do tenant solicitado."""
    ids_empresas: set[str] = set()
    for empresa in empresas:
        tenant_id = empresa.get("tenant_id")
        if str(tenant_id) != str(tenant_origem):
            raise RuntimeError(
                "Falha de segurança: empresa "
                f"{empresa.get('id')} pertence ao tenant {tenant_id}, não a {tenant_origem}."
            )
        ids_empresas.add(str(empresa["id"]))

    ids_planos = {str(plano["id"]) for plano in planos}
    for plano in planos:
        empresa_id = str(plano.get("empresa_id"))
        if empresa_id not in ids_empresas:
            raise RuntimeError(
                "Falha de segurança: plano de contas "
                f"{plano.get('id')} referencia empresa {empresa_id} fora do tenant "
                f"{tenant_origem}."
            )
        pai_id = plano.get("pai_id")
        if pai_id is not None and str(pai_id) not in ids_planos:
            raise RuntimeError(
                "Falha de segurança: plano de contas "
                f"{plano.get('id')} referencia plano pai {pai_id} fora do tenant "
                f"{tenant_origem}."
            )


async def exportar(tenant_origem: UUID, tenant_destino: UUID, arquivo: Path) -> None:
    from sqlalchemy import text

    from src.db.session import _get_factory

    factory = _get_factory()

    async with factory() as db:
        tenant_existe = (
            await db.execute(
                text("SELECT id FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_origem},
            )
        ).first()
        if tenant_existe is None:
            raise RuntimeError(f"Tenant de origem não encontrado: {tenant_origem}")

        empresas = (
            await db.execute(
                text(
                    "SELECT * FROM empresas "
                    "WHERE tenant_id = :tenant_id ORDER BY created_at"
                ),
                {"tenant_id": tenant_origem},
            )
        ).mappings().all()

        planos = (
            await db.execute(
                text(
                    "SELECT pc.* FROM plano_contas AS pc "
                    "INNER JOIN empresas AS e ON e.id = pc.empresa_id "
                    "WHERE e.tenant_id = :tenant_id ORDER BY pc.created_at"
                ),
                {"tenant_id": tenant_origem},
            )
        ).mappings().all()

    validar_escopo(empresas, planos, tenant_origem)

    print(f"Tenant origem : {tenant_origem}")
    print(f"Tenant destino: {tenant_destino}")
    print(f"Empresas      : {len(empresas)}")
    print(f"Plano contas  : {len(planos)}")

    with arquivo.open("w", encoding="utf-8") as output:
        output.write("-- Migração: empresas + plano_contas\n")
        output.write(f"-- Tenant origem: {tenant_origem}\n")
        output.write(f"-- Tenant destino: {tenant_destino}\n\n")
        output.write("BEGIN;\n\n")

        output.write("-- ===== EMPRESAS =====\n")
        for row in empresas:
            values = dict(row)
            values["tenant_id"] = tenant_destino
            cols = ", ".join(values.keys())
            vals = ", ".join(to_sql_val(value) for value in values.values())
            output.write(
                f"INSERT INTO empresas ({cols}) VALUES ({vals}) "
                "ON CONFLICT (id) DO NOTHING;\n"
            )

        output.write("\n-- ===== PLANO DE CONTAS =====\n")
        for row in planos:
            values = dict(row)
            cols = ", ".join(values.keys())
            vals = ", ".join(to_sql_val(value) for value in values.values())
            output.write(
                f"INSERT INTO plano_contas ({cols}) VALUES ({vals}) "
                "ON CONFLICT (id) DO NOTHING;\n"
            )

        output.write("\nCOMMIT;\n")

    size_mb = os.path.getsize(arquivo) / 1024 / 1024
    print(f"Arquivo gerado: {arquivo} ({size_mb:.1f} MB)")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(exportar(args.tenant_origem, args.tenant_destino, args.arquivo))
    except Exception as exc:
        print(f"ERRO: exportação abortada: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
