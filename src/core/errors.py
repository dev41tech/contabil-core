"""Hierarquia de erros tipados da aplicação.

Regras:
- Todo erro de domínio herda de DomainError.
- Todo erro de integração externa herda de ExternalServiceError.
- O handler global converte para ErrorResponse JSON.
- Nenhum 500 sem contexto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────── Base


@dataclass
class AppError(Exception):
    """Raiz de todos os erros da aplicação."""

    message: str
    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# ─────────────────────────────────────────────────────────────── Domínio


class DomainError(AppError):
    """Erro de regra de negócio — 4xx."""
    pass


@dataclass
class ValidationError(DomainError):
    message: str = "Dados inválidos."
    code: str = "VALIDATION_ERROR"
    http_status: int = 422
    fields: list[dict[str, str]] = field(default_factory=list)


@dataclass
class NotFoundError(DomainError):
    message: str = "Recurso não encontrado."
    code: str = "NOT_FOUND"
    http_status: int = 404


@dataclass
class ConflictError(DomainError):
    message: str = "Conflito com recurso existente."
    code: str = "CONFLICT"
    http_status: int = 409


@dataclass
class ForbiddenError(DomainError):
    message: str = "Sem permissão para esta operação."
    code: str = "FORBIDDEN"
    http_status: int = 403


@dataclass
class PayloadTooLargeError(DomainError):
    message: str = "Arquivo maior que o limite permitido."
    code: str = "PAYLOAD_TOO_LARGE"
    http_status: int = 413


# ─────────────────────────────────────────────────────────────── Auth


@dataclass
class AuthError(DomainError):
    message: str = "Não autenticado."
    code: str = "UNAUTHORIZED"
    http_status: int = 401


@dataclass
class InvalidCredentialsError(AuthError):
    message: str = "Credenciais inválidas."
    code: str = "INVALID_CREDENTIALS"


@dataclass
class TokenExpiredError(AuthError):
    message: str = "Sessão expirada. Faça login novamente."
    code: str = "TOKEN_EXPIRED"


@dataclass
class InvalidTokenError(AuthError):
    message: str = "Token inválido."
    code: str = "INVALID_TOKEN"


@dataclass
class TenantAccessDeniedError(ForbiddenError):
    message: str = "Acesso negado a esta empresa."
    code: str = "TENANT_ACCESS_DENIED"


# ─────────────────────────────────────────────────────────────── Integrações


class ExternalServiceError(AppError):
    """Erro de serviço externo (Pluggy, SEFAZ, ERP)."""
    pass


@dataclass
class PluggyUnavailableError(ExternalServiceError):
    message: str = "Serviço Pluggy indisponível."
    code: str = "PLUGGY_UNAVAILABLE"
    http_status: int = 503


@dataclass
class SefazUnavailableError(ExternalServiceError):
    message: str = "Serviço SEFAZ indisponível."
    code: str = "SEFAZ_UNAVAILABLE"
    http_status: int = 503


# ─────────────────────────────────────────────────────────────── Domínio específico


@dataclass
class RegraJaExisteError(ConflictError):
    message: str = "Já existe uma regra com este histórico para esta conta bancária."
    code: str = "REGRA_JA_EXISTE"


@dataclass
class ExtratoJaImportadoError(ConflictError):
    message: str = "Este extrato já foi importado anteriormente."
    code: str = "EXTRATO_JA_IMPORTADO"


@dataclass
class EmpresaNaoEncontradaError(NotFoundError):
    message: str = "Empresa não encontrada."
    code: str = "EMPRESA_NAO_ENCONTRADA"
