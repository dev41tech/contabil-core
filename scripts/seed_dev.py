"""Seed de dados para desenvolvimento.

Cria um escritório + usuário admin + 3 empresas de exemplo.

Uso:
    python scripts/seed_dev.py --i-know-what-im-doing
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import get_settings
from src.core.security import hash_password
from src.db.models import Empresa, Tenant, Usuario


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
        # Escritório
        tenant = Tenant(
            nome="41 Contábil Ltda",
            cnpj="41.000.000/0001-00",
            plano="premium",
        )
        db.add(tenant)
        await db.flush()

        # Usuário admin
        admin = Usuario(
            tenant_id=tenant.id,
            email="admin@41contabil.com.br",
            nome="Luiz Admin",
            senha_hash=hash_password(admin_password),
            role="admin",
        )
        db.add(admin)

        # Empresas de exemplo
        empresas = [
            Empresa(
                tenant_id=tenant.id,
                razao_social="DECATEC LTDA",
                cnpj="12.345.678/0001-90",
                regime_tributario="simples_nacional",
            ),
            Empresa(
                tenant_id=tenant.id,
                razao_social="AXEL INDÚSTRIA LTDA",
                cnpj="98.765.432/0001-10",
                regime_tributario="lucro_presumido",
            ),
            Empresa(
                tenant_id=tenant.id,
                razao_social="BLD LOGÍSTICA LTDA",
                cnpj="11.222.333/0001-44",
                regime_tributario="lucro_real",
            ),
        ]
        for e in empresas:
            db.add(e)

        await db.commit()

        print(f"✅ Tenant criado: {tenant.id}")
        print("✅ Admin: admin@41contabil.com.br")
        print(f"✅ {len(empresas)} empresas criadas")
        print(f"\n📋 Para fazer login use:")
        print(f'   tenant_id: "{tenant.id}"')
        print(f'   email: "admin@41contabil.com.br"')
        print(f'   senha: "{admin_password}"')

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cria dados locais de desenvolvimento.")
    parser.add_argument(
        "--i-know-what-im-doing",
        action="store_true",
        help="Confirma que DATABASE_URL aponta para um banco descartável.",
    )
    args = parser.parse_args()
    asyncio.run(seed(confirmed=args.i_know_what_im_doing))
