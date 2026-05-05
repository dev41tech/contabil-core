"""Utilitários de segurança: JWT, cookies, CSRF, hash de senha."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import bcrypt
from jose import JWTError, jwt

from src.core.config import get_settings
from src.core.errors import InvalidTokenError, TokenExpiredError

# ─────────────────────────────────────────────────────────────── Constantes

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"

COOKIE_ACCESS = "access_token"
COOKIE_REFRESH = "refresh_token"
CSRF_HEADER = "X-CSRF-Token"


# ─────────────────────────────────────────────────────────────── Senha


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ─────────────────────────────────────────────────────────────── JWT


def _create_token(payload: dict[str, Any], expires_delta: timedelta) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        **payload,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)


def create_access_token(user_id: UUID, tenant_id: UUID, role: str) -> str:
    settings = get_settings()
    return _create_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": role,
            "type": ACCESS_TOKEN_TYPE,
        },
        timedelta(minutes=settings.access_token_ttl_minutes),
    )


def create_refresh_token(user_id: UUID, tenant_id: UUID) -> str:
    settings = get_settings()
    return _create_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "type": REFRESH_TOKEN_TYPE,
            "jti": secrets.token_hex(16),  # ID único para revogação
        },
        timedelta(days=settings.refresh_token_ttl_days),
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decodifica e valida um JWT. Lança erros tipados."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[ALGORITHM],
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise TokenExpiredError()
    except JWTError:
        raise InvalidTokenError()


def decode_access_token(token: str) -> dict[str, Any]:
    payload = decode_token(token)
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise InvalidTokenError(message="Tipo de token inválido.")
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    payload = decode_token(token)
    if payload.get("type") != REFRESH_TOKEN_TYPE:
        raise InvalidTokenError(message="Tipo de token inválido.")
    return payload


# ─────────────────────────────────────────────────────────────── CSRF


def generate_csrf_token() -> str:
    return secrets.token_hex(32)


# ─────────────────────────────────────────────────────────────── Cookie helpers


def make_cookie_kwargs(secure: bool | None = None) -> dict[str, Any]:
    """Parâmetros padrão para cookies de autenticação."""
    settings = get_settings()
    use_secure = secure if secure is not None else settings.cookie_secure
    return {
        "httponly": True,
        "secure": use_secure,
        "samesite": settings.cookie_samesite,
        "domain": settings.cookie_domain,
    }
