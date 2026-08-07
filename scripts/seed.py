"""Seed de desenvolvimento -- cria tenant, admin e empresa demo.

Uso:
    python scripts/seed.py --i-know-what-im-doing

Variaveis necessarias (mesmo que para rodar o servidor):
    DATABASE_URL  SECRET_KEY  ENVIRONMENT=development|test
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import uuid

# Garante que o src/ esta no path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import get_settings
from src.core.security import hash_password
from src.db.models import (
    AgenciaBancaria,
    Empresa,
    PlanoConta,
    Tenant,
    Usuario,
)


async def seed(*, confirmed: bool = False) -> None:
    settings = get_settings()
    if settings.environment not in {"development", "test"}:
        raise SystemExit(
            "Seed recusado: ENVIRONMENT deve ser development ou test."
        )
    if not confirmed:
        raise SystemExit(
            "Seed recusado: confirme o destino com --i-know-what-im-doing."
        )

    admin_password = secrets.token_urlsafe(18)
    engine = create_async_engine(str(settings.database_url), echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        # -- 1. Tenant
        tenant = Tenant(
            id=uuid.uuid4(),
            nome="41 Contabil LTDA",
            cnpj="41.000.000/0001-00",
            plano="premium",
        )
        db.add(tenant)
        await db.flush()
        print(f"OK Tenant criado: {tenant.nome} ({tenant.id})")

        # -- 2. Usuario admin
        admin = Usuario(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="admin@contabil.dev",
            nome="Administrador",
            senha_hash=hash_password(admin_password),
            role="admin",
        )
        db.add(admin)
        await db.flush()
        print(f"OK Usuario criado: {admin.email}")

        # -- 3. Empresa demo
        empresa = Empresa(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            razao_social="DECATEC COMERCIO LTDA",
            cnpj="12.345.678/0001-90",
            regime_tributario="simples_nacional",
        )
        db.add(empresa)
        await db.flush()
        print(f"OK Empresa criada: {empresa.razao_social} ({empresa.id})")

        # -- 4. Plano de Contas basico
        # (codigo, descricao, tipo)
        contas = [
            ("1",     "ATIVO",                    "ativo"),
            ("1.1",   "ATIVO CIRCULANTE",          "ativo"),
            ("1.1.1", "Caixa e Equivalentes",      "ativo"),
            ("1.1.2", "Bancos Conta Movimento",    "ativo"),
            ("1.1.3", "Contas a Receber",          "ativo"),
            ("2",     "PASSIVO",                   "passivo"),
            ("2.1",   "PASSIVO CIRCULANTE",        "passivo"),
            ("2.1.1", "Fornecedores a Pagar",      "passivo"),
            ("2.1.2", "Obrigacoes Tributarias",    "passivo"),
            ("3",     "RECEITAS",                  "receita"),
            ("3.1",   "Receitas Operacionais",     "receita"),
            ("3.1.1", "Vendas de Produtos",        "receita"),
            ("3.1.2", "Prestacao de Servicos",     "receita"),
            ("4",     "DESPESAS",                  "despesa"),
            ("4.1",   "Despesas Operacionais",     "despesa"),
            ("4.1.1", "Folha de Pagamento",        "despesa"),
            ("4.1.2", "Aluguel",                   "despesa"),
            ("4.1.3", "Energia Eletrica",          "despesa"),
            ("4.1.4", "Fornecedores Diversos",     "despesa"),
        ]

        conta_map: dict[str, PlanoConta] = {}
        for codigo, descricao, tipo in contas:
            partes = codigo.split(".")
            pai_codigo = ".".join(partes[:-1]) if len(partes) > 1 else None
            pai_id = conta_map[pai_codigo].id if pai_codigo else None

            conta = PlanoConta(
                id=uuid.uuid4(),
                empresa_id=empresa.id,
                pai_id=pai_id,
                codigo=codigo,
                descricao=descricao,
                tipo=tipo,
            )
            db.add(conta)
            await db.flush()
            conta_map[codigo] = conta

        print(f"OK Plano de contas criado: {len(contas)} contas")

        # -- 5. Agencia bancaria demo
        agencia = AgenciaBancaria(
            id=uuid.uuid4(),
            empresa_id=empresa.id,
            banco_sigla="BRADESCO",
            agencia="0001",
            numero="12345",
            digito="6",
        )
        db.add(agencia)
        await db.flush()
        print(f"OK Agencia criada: {agencia.id}")

        await db.commit()

    await engine.dispose()

    print("\n" + "=" * 55)
    print("  SEED CONCLUIDO - dados para teste:")
    print("=" * 55)
    print(f"  Login:    admin@contabil.dev")
    print(f"  Senha:    {admin_password}")
    print(f"  Tenant:   {tenant.id}")
    print(f"  Empresa:  {empresa.id}")
    print(f"  Agencia:  {agencia.id}")
    print("=" * 55)
    print("\n  Swagger: http://localhost:8000/api/docs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cria dados locais de desenvolvimento.")
    parser.add_argument(
        "--i-know-what-im-doing",
        action="store_true",
        help="Confirma que DATABASE_URL aponta para um banco descartável.",
    )
    args = parser.parse_args()
    asyncio.run(seed(confirmed=args.i_know_what_im_doing))
