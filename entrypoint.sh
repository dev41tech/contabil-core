#!/bin/sh
set -e

echo "=== Contabil Core — startup ==="

# ── Aguarda o PostgreSQL ──────────────────────────────────────────────────────
# Usa um script Python separado para evitar problemas de quoting + set -e no until
echo "Aguardando banco de dados..."

RETRIES=30
i=0
while [ $i -lt $RETRIES ]; do
  if python - <<'PYEOF'
import asyncio, sys, os

async def ping():
    try:
        import asyncpg
        raw = os.environ.get("DATABASE_URL", "")
        # Normaliza URL: postgres:// ou postgresql:// → asyncpg nativo
        url = (raw
            .replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgres://", "postgresql://"))
        conn = await asyncpg.connect(url, timeout=5)
        await conn.close()
    except Exception as e:
        print(f"  DB não disponível: {e}", file=sys.stderr)
        sys.exit(1)

asyncio.run(ping())
PYEOF
  then
    echo "Banco disponível."
    break
  fi
  i=$((i + 1))
  if [ $i -ge $RETRIES ]; then
    echo "ERRO: banco não ficou disponível após $RETRIES tentativas." >&2
    exit 1
  fi
  echo "  Tentativa $i/$RETRIES — aguardando 3s..."
  sleep 3
done

# ── Migrations Alembic ────────────────────────────────────────────────────────
echo "Executando migrations..."
alembic upgrade head
echo "Migrations OK."

# ── Inicia uvicorn ────────────────────────────────────────────────────────────
echo "Iniciando uvicorn (workers=2)..."
exec uvicorn main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 2 \
  --log-config null \
  --no-access-log
