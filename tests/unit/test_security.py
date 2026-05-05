"""Testes unitários do módulo de segurança — sem banco, sem I/O."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from src.core.errors import InvalidTokenError, TokenExpiredError
from src.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)


# ── Senha


def test_hash_password_nao_retorna_plaintext():
    hashed = hash_password("minha_senha")
    assert hashed != "minha_senha"
    assert len(hashed) > 20


def test_verify_password_correta():
    hashed = hash_password("minha_senha")
    assert verify_password("minha_senha", hashed) is True


def test_verify_password_errada():
    hashed = hash_password("minha_senha")
    assert verify_password("senha_errada", hashed) is False


# ── Access token


def test_create_and_decode_access_token():
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    token = create_access_token(user_id, tenant_id, "admin")
    payload = decode_access_token(token)

    assert payload["sub"] == str(user_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["role"] == "admin"
    assert payload["type"] == "access"


def test_decode_refresh_token_como_access_falha():
    """Refresh token não deve ser aceito como access token."""
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    refresh = create_refresh_token(user_id, tenant_id)

    with pytest.raises(InvalidTokenError):
        decode_access_token(refresh)


# ── Refresh token


def test_create_and_decode_refresh_token():
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    token = create_refresh_token(user_id, tenant_id)
    payload = decode_refresh_token(token)

    assert payload["sub"] == str(user_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["type"] == "refresh"
    assert "jti" in payload


def test_dois_refresh_tokens_tem_jti_diferente():
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    t1 = decode_refresh_token(create_refresh_token(user_id, tenant_id))
    t2 = decode_refresh_token(create_refresh_token(user_id, tenant_id))

    assert t1["jti"] != t2["jti"]


def test_token_invalido_lanca_erro():
    with pytest.raises(InvalidTokenError):
        decode_access_token("nao.e.um.jwt.valido")


def test_token_adulterado_lanca_erro():
    token = create_access_token(uuid.uuid4(), uuid.uuid4(), "admin")
    partes = token.split(".")
    partes[1] = "payloadAdulterado"
    adulterado = ".".join(partes)

    with pytest.raises(InvalidTokenError):
        decode_access_token(adulterado)
