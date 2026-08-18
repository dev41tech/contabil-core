# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Dependências de sistema para compilar psycopg2 e pymupdf
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Instala dependências Python numa layer separada (cache-friendly)
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir -e .

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Runtime libs para psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copia site-packages compilados e binários
COPY --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=builder /usr/local/bin /usr/local/bin

# Copia código da aplicação
COPY . .

# SHA do commit desta imagem — exposto em GET /api/health e /api/health/live.
# Prioridade: --build-arg GIT_COMMIT explícito (se o pipeline passar) senão
# resolvido automaticamente aqui a partir do próprio .git/ do build context —
# o pipeline de deploy em uso não estava passando o build-arg, então isso
# sempre respondia "unknown". .git/ é removido antes do fim da imagem: entra
# só pra essa leitura, não fica na imagem final.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
RUN if [ "$GIT_COMMIT" = "unknown" ]; then \
        python scripts/resolve_git_commit.py > /app/.git_commit 2>/dev/null || echo unknown > /app/.git_commit; \
    else \
        echo "$GIT_COMMIT" > /app/.git_commit; \
    fi \
 && rm -rf .git
ENV GIT_COMMIT_FALLBACK_FILE=/app/.git_commit

# Entrypoint com permissão de execução
RUN chmod +x entrypoint.sh

# Usuário não-root para segurança
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 3012

# Liveness, não readiness: /api/health devolve 503 quando o banco está fora e
# não deve fazer o Docker matar um container que está saudável.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3012/api/health/live')"

ENTRYPOINT ["./entrypoint.sh"]
