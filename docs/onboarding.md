# Onboarding — Contabil Core

## Pré-requisitos

- Python 3.12+
- Docker + Docker Compose
- Git

## Setup em 5 minutos

```bash
# 1. Clone e entre no diretório
cd contabil-core

# 2. Rode o setup automático
bash scripts/setup_dev.sh

# 3. Inicie a API
source .venv/bin/activate
python main.py
```

Acesse: **http://localhost:8000/api/docs**

## Estrutura do projeto

```
src/
  api/v1/         # Routers FastAPI — handlers HTTP apenas
  domain/         # Lógica de negócio pura — sem HTTP, sem banco direto
  adapters/       # Integrações externas (Pluggy, SEFAZ, ERP)
  db/             # SQLAlchemy models + Alembic migrations
  schemas/        # Pydantic DTOs de entrada e saída
  core/           # Config, logging, segurança, erros, contexto
  jobs/           # Workers assíncronos (NEO, importação)

tests/
  unit/           # Domínio puro — sem banco, < 1ms por teste
  integration/    # Com banco SQLite em memória — sem Docker necessário
  smoke/          # App sobe e responde — sem banco
  e2e/            # Playwright — fluxos completos (requer app rodando)
```

## Rodando testes

```bash
# Todos os testes (sem Docker necessário)
pytest tests/

# Unitários apenas
pytest tests/unit/

# Com cobertura
pytest tests/ --cov=src --cov-report=html
```

## Migrações de banco

```bash
# Criar nova migration após alterar models
alembic revision --autogenerate -m "descricao_da_mudanca"

# Aplicar migrations
alembic upgrade head

# Reverter última
alembic downgrade -1
```

## Variáveis de ambiente

Copie `.env.example` para `.env` e configure:

| Variável | Obrigatório | Descrição |
|---|---|---|
| `SECRET_KEY` | Sim | Mínimo 32 chars — assina JWT |
| `DATABASE_URL` | Sim | postgresql+asyncpg://... |
| `REDIS_URL` | Não | redis://localhost:6379/0 |
| `ENVIRONMENT` | Não | development/staging/production |
| `COOKIE_SECURE` | Não | false em dev, true em prod |

## Convenções

- **Todo erro de domínio herda de `DomainError`** — nunca raise `Exception` bruto
- **Logs com contexto** — use `logger.info("acao.feita", campo=valor)` não `logger.info(f"acao")`
- **Queries filtram por `tenant_id`** — nunca buscar sem escopo de tenant
- **Mutations precisam de CSRF** — header `X-CSRF-Token` em POST/PUT/PATCH/DELETE
- **Nenhum secret no código** — use variável de ambiente

## Adicionando um novo módulo

1. Crie `src/domain/{modulo}/service.py` com a lógica de negócio
2. Crie `src/schemas/{modulo}.py` com os DTOs Pydantic
3. Crie `src/api/v1/{modulo}.py` com o router FastAPI
4. Registre o router em `src/api/v1/__init__.py`
5. Adicione tests em `tests/unit/` e `tests/integration/`
6. Documente a decisão em `DECISIONS.md` se houver escolha não óbvia
