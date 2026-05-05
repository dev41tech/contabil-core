#!/bin/sh
set -e

echo "=== Contabil Core — startup ==="

# Aguarda o PostgreSQL ficar disponível
echo "Aguardando banco de dados..."
until python -c "
import asyncio, sys
async def ping():
    try:
        import asyncpg
        url = '${DATABASE_URL}'.replace('postgresql+asyncpg://', 'postgresql://')
        conn = await asyncpg.connect(url, timeout=5)
        await conn.close()
        print('Banco disponível.')
    except Exception as e:
        print(f'Banco indisponível: {e}', file=sys.stderr)
        sys.exit(1)
asyncio.run(ping())
"; do
  echo "  Banco não disponível — aguardando 3s..."
  sleep 3
done

# Roda as migrations Alembic
echo "Executando migrations..."
alembic upgrade head
echo "Migrations OK."

# Inicia o servidor
echo "Iniciando uvicorn..."
exec uvicorn main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 2 \
  --log-config null \
  --no-access-log
