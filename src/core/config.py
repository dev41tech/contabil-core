"""Configuração central via Pydantic Settings.

Falha rápido na inicialização se alguma variável obrigatória estiver ausente.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ App
    app_name: str = "contabil-core"
    app_version: str = "0.1.0"
    # Versão do código que virou a imagem — SHA do commit ou, quando o build
    # não tem `.git/`, o fingerprint do fonte. `app_version` não muda entre
    # deploys, então é isto que responde "meu deploy subiu?".
    #
    # Preenchido pelo Dockerfile em `GIT_COMMIT_FALLBACK_FILE`, e não pela
    # variável `GIT_COMMIT` direto: um `RUN` não consegue alterar um `ENV` já
    # definido na imagem, então o valor resolvido durante o build só chega até
    # aqui por arquivo. Era exatamente essa a lacuna que mantinha o health
    # respondendo "unknown" mesmo com o resolvedor funcionando.
    git_commit: str = "unknown"
    git_commit_fallback_file: str | None = None
    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False

    # ------------------------------------------------------------------ Segurança
    secret_key: SecretStr = Field(..., description="Chave para assinar JWT — mínimo 32 chars")
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    csrf_token_ttl_minutes: int = 60
    enable_setup_endpoint: bool = False
    setup_bootstrap_secret: SecretStr | None = None

    # ------------------------------------------------------------------ Banco
    database_url: PostgresDsn = Field(..., description="postgresql+asyncpg://user:pass@host/db")

    # ------------------------------------------------------------------ Redis
    redis_url: str = "redis://localhost:6379/0"

    # ------------------------------------------------------------------ CORS
    # Armazenado como string para que o pydantic-settings não tente json.loads()
    # automaticamente. Use a property `allowed_origins_list` ou acesse via str.split.
    # Formatos aceitos no .env:
    #   ALLOWED_ORIGINS=http://localhost:4200,http://localhost:3000
    #   ALLOWED_ORIGINS=["http://localhost:4200","http://localhost:3000"]
    allowed_origins: str = "http://localhost:4200"

    @property
    def allowed_origins_list(self) -> list[str]:
        """Retorna ALLOWED_ORIGINS como lista — aceita vírgula ou JSON array."""
        v = self.allowed_origins.strip()
        if v.startswith("["):
            import json
            return json.loads(v)
        return [o.strip() for o in v.split(",") if o.strip()]

    # ------------------------------------------------------------------ Open Banking (Pluggy)
    pluggy_client_id: str | None = None
    pluggy_client_secret: SecretStr | None = None

    @property
    def pluggy_enabled(self) -> bool:
        return bool(self.pluggy_client_id and self.pluggy_client_secret)

    # ------------------------------------------------------------------ OpenAI (OCR de PDF via Vision)
    openai_api_key: SecretStr | None = None

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    # ------------------------------------------------------------------ Rate limit
    rate_limit_per_ip: int = 100        # req/min
    rate_limit_per_tenant: int = 1000   # req/min
    rate_limit_per_identity: int = 10   # tentativas de login/min

    # ------------------------------------------------------------------ Upload
    # Todos os parsers (Razão em PDF/XLSX, OFX, CSV) carregam o arquivo inteiro
    # em memória, então o limite é o que separa um upload grande de um OOM.
    # Manter abaixo do `client_max_body_size` do nginx do contabil-front (50M),
    # para que a recusa venha daqui com JSON tipado, não do nginx em HTML.
    max_upload_mb: int = 25

    # PDF bancário: processamento síncrono pesado e, opcionalmente, envio a IA.
    pdf_max_pages: int = 25
    pdf_parse_timeout_seconds: int = 60
    pdf_max_ai_calls: int = 10
    allow_financial_data_to_openai: bool = False

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    # ------------------------------------------------------------------ Cookie
    cookie_domain: str | None = None    # None = mesmo domínio
    cookie_secure: bool = True          # False em desenvolvimento
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"

    @field_validator("secret_key", mode="before")
    @classmethod
    def secret_key_min_length(cls, v: str) -> str:
        if len(str(v)) < 32:
            raise ValueError("secret_key deve ter no mínimo 32 caracteres")
        return v

    @model_validator(mode="after")
    def resolver_git_commit_por_arquivo(self) -> Settings:
        """Lê o arquivo escrito no build quando `GIT_COMMIT` não veio explícito.

        Build arg explícito continua tendo prioridade — se alguém passar
        `--build-arg GIT_COMMIT`, é essa a resposta.
        """
        if self.git_commit != "unknown" or not self.git_commit_fallback_file:
            return self
        try:
            with open(self.git_commit_fallback_file, encoding="utf-8") as f:
                valor = f.read().strip()
        except OSError:
            # Arquivo ausente ou ilegível não pode derrubar a aplicação: o
            # health degradado para "unknown" é ruim, ficar sem subir é pior.
            return self
        if valor:
            object.__setattr__(self, "git_commit", valor)
        return self

    @model_validator(mode="after")
    def setup_exige_segredo(self) -> Settings:
        if self.enable_setup_endpoint and (
            self.setup_bootstrap_secret is None
            or not self.setup_bootstrap_secret.get_secret_value()
        ):
            raise ValueError(
                "setup_bootstrap_secret é obrigatório quando enable_setup_endpoint=true"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Singleton — lido uma vez, cacheado para sempre."""
    return Settings()
