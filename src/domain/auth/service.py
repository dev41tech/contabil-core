"""Serviço de autenticação.

Responsabilidades:
- Login: valida credenciais, cria access + refresh token, persiste refresh.
- Refresh: valida refresh token, revoga o antigo, emite novos tokens.
- Logout: revoga o refresh token ativo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.errors import InvalidCredentialsError, InvalidTokenError, TokenExpiredError
from src.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    verify_password,
)
from src.db.models import RefreshToken, Tenant, Usuario

logger = structlog.get_logger(__name__)

# Hash bcrypt válido usado quando o e-mail/tenant não existe. Assim uma tentativa
# inválida sempre paga o mesmo custo de bcrypt e não revela a existência do usuário.
_DUMMY_PASSWORD_HASH = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6Ttx7EZ6YkYxja1o8dWfJwY8dQXgG"


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._settings = get_settings()

    async def login(self, email: str, senha: str, tenant_id: UUID) -> tuple[str, str]:
        """
        Autentica o usuário e retorna (access_token, refresh_token).
        Lança InvalidCredentialsError se credenciais inválidas.
        """
        result = await self._db.execute(
            select(Usuario)
            .join(Tenant, Tenant.id == Usuario.tenant_id)
            .where(
                Usuario.email == email.lower().strip(),
                Usuario.tenant_id == tenant_id,
                Usuario.ativo == True,
                Tenant.ativo == True,
            )
        )
        user = result.scalar_one_or_none()
        password_ok = verify_password(
            senha,
            user.senha_hash if user else _DUMMY_PASSWORD_HASH,
        )

        if not user or not password_ok:
            # Mesma mensagem independente do motivo — evita enumeração de usuários
            logger.warning("auth.login.failed", email=email, tenant_id=str(tenant_id))
            raise InvalidCredentialsError()

        access_token = create_access_token(user.id, tenant_id, user.role)
        refresh_token = create_refresh_token(user.id, tenant_id)

        await self._persist_refresh_token(user.id, refresh_token)

        logger.info("auth.login.success", user_id=str(user.id), tenant_id=str(tenant_id))
        return access_token, refresh_token

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        """
        Emite novos tokens a partir de um refresh token válido.
        Revoga o refresh token usado (rotação).
        """
        try:
            payload = decode_refresh_token(refresh_token)
        except TokenExpiredError:
            raise
        except Exception:
            raise InvalidTokenError()

        jti = payload.get("jti", "")
        user_id = UUID(payload["sub"])
        tenant_id = UUID(payload["tenant_id"])

        # Consome o token em um único UPDATE condicional. Requisições concorrentes
        # não conseguem observar e usar o mesmo token antes da revogação.
        result = await self._db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.jti == jti,
                RefreshToken.usuario_id == user_id,
                RefreshToken.revogado == False,
            )
            .values(revogado=True)
            .returning(RefreshToken.usuario_id)
        )
        consumed_user_id = result.scalar_one_or_none()
        if consumed_user_id is None:
            logger.warning("auth.refresh.invalid_or_revoked", jti=jti)
            raise InvalidTokenError(message="Refresh token inválido ou já utilizado.")

        # Busca role atual e também invalida sessões de tenant desativado.
        result = await self._db.execute(
            select(Usuario)
            .join(Tenant, Tenant.id == Usuario.tenant_id)
            .where(
                Usuario.id == user_id,
                Usuario.tenant_id == tenant_id,
                Usuario.ativo == True,
                Tenant.ativo == True,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise InvalidTokenError(message="Usuário não encontrado.")

        new_access = create_access_token(user_id, tenant_id, user.role)
        new_refresh = create_refresh_token(user_id, tenant_id)
        await self._persist_refresh_token(user_id, new_refresh)

        logger.info("auth.refresh.success", user_id=str(user_id))
        return new_access, new_refresh

    async def logout(self, refresh_token: str) -> None:
        """Revoga o refresh token, invalidando a sessão."""
        try:
            payload = decode_refresh_token(refresh_token)
            jti = payload.get("jti", "")
            await self._db.execute(
                update(RefreshToken)
                .where(RefreshToken.jti == jti)
                .values(revogado=True)
            )
            logger.info("auth.logout", jti=jti)
        except Exception:
            # Logout nunca deve falhar visivelmente para o usuário
            logger.warning("auth.logout.token_invalid")

    async def _persist_refresh_token(self, user_id: UUID, token: str) -> None:
        payload = decode_refresh_token(token)
        expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
        rt = RefreshToken(
            usuario_id=user_id,
            jti=payload["jti"],
            expires_at=expires_at,
        )
        self._db.add(rt)
        await self._db.flush()
