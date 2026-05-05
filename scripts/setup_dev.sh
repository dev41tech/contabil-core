#!/usr/bin/env bash
# Setup do ambiente de desenvolvimento local
set -euo pipefail

echo "🔧 Configurando ambiente de desenvolvimento..."

# 1. Verifica Python 3.12+
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python: $python_version"

# 2. Cria virtualenv se não existir
if [ ! -d ".venv" ]; then
    echo "Criando virtualenv..."
    python3 -m venv .venv
fi

# 3. Ativa e instala dependências
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]" aiosqlite

# 4. Copia .env se não existir
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ".env criado a partir do .env.example"
    echo "⚠️  Edite o .env e defina a SECRET_KEY antes de iniciar!"
fi

# 5. Sobe infraestrutura local
echo "Subindo PostgreSQL + Redis..."
docker compose up -d db redis

echo "Aguardando banco ficar pronto..."
sleep 5

# 6. Roda migrations
echo "Rodando migrations..."
alembic upgrade head

echo ""
echo "✅ Ambiente pronto!"
echo ""
echo "Para iniciar a API:"
echo "  source .venv/bin/activate"
echo "  python main.py"
echo ""
echo "Acesse: http://localhost:8000/api/docs"
