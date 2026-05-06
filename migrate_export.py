"""Exporta empresas + plano_contas do banco local para SQL de migração."""
import sys, asyncio
sys.path.insert(0, '.')

TENANT_ID_PROD = '54d0dbf4-3220-4f75-90ff-710031635d2d'
OUTPUT_FILE = 'migrate_prod.sql'


def to_sql_val(v):
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    s = str(v).replace("'", "''")
    return f"'{s}'"


async def main():
    from src.db.session import _get_factory
    from sqlalchemy import text

    factory = _get_factory()

    async with factory() as db:
        row = (await db.execute(text('SELECT id FROM tenants LIMIT 1'))).first()
        tenant_local = str(row[0])
        print(f'Tenant local : {tenant_local}')
        print(f'Tenant prod  : {TENANT_ID_PROD}')

        empresas = (await db.execute(
            text('SELECT * FROM empresas ORDER BY created_at')
        )).mappings().all()
        print(f'Empresas     : {len(empresas)}')

        plano = (await db.execute(
            text('SELECT * FROM plano_contas ORDER BY created_at')
        )).mappings().all()
        print(f'Plano contas : {len(plano)}')

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('-- Migração: empresas + plano_contas\n')
        f.write(f'-- Tenant produção: {TENANT_ID_PROD}\n\n')
        f.write('BEGIN;\n\n')

        f.write('-- ===== EMPRESAS =====\n')
        for r in empresas:
            d = dict(r)
            if 'tenant_id' in d:
                d['tenant_id'] = TENANT_ID_PROD
            cols = ', '.join(d.keys())
            vals = ', '.join(to_sql_val(v) for v in d.values())
            f.write(f'INSERT INTO empresas ({cols}) VALUES ({vals}) ON CONFLICT (id) DO NOTHING;\n')

        f.write('\n-- ===== PLANO DE CONTAS =====\n')
        for r in plano:
            d = dict(r)
            if 'tenant_id' in d:
                d['tenant_id'] = TENANT_ID_PROD
            cols = ', '.join(d.keys())
            vals = ', '.join(to_sql_val(v) for v in d.values())
            f.write(f'INSERT INTO plano_contas ({cols}) VALUES ({vals}) ON CONFLICT (id) DO NOTHING;\n')

        f.write('\nCOMMIT;\n')

    import os
    size_mb = os.path.getsize(OUTPUT_FILE) / 1024 / 1024
    print(f'Arquivo gerado: {OUTPUT_FILE} ({size_mb:.1f} MB)')


asyncio.run(main())
