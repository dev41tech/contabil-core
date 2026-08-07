"""Rate limit distribuído para endpoints sensíveis de autenticação."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.config import Settings
from src.core.errors import RateLimitExceededError, RateLimitUnavailableError

logger = structlog.get_logger(__name__)

_INCREMENT_SCRIPT = """
for i, key in ipairs(KEYS) do
    local current = tonumber(redis.call('GET', key) or '0')
    if current >= tonumber(ARGV[i]) then
        return i
    end
end
for i, key in ipairs(KEYS) do
    local current = redis.call('INCR', key)
    if current == 1 then
        redis.call('EXPIRE', key, 120)
    end
end
return 0
"""


class LoginRateLimiter:
    """Limita login por IP, tenant e identidade em uma janela fixa de um minuto.

    Em produção o Redis é obrigatório para que o limite seja compartilhado por
    todos os processos/containers. Desenvolvimento e testes usam fallback local
    quando o Redis não está disponível.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        self._local_counts: dict[str, int] = defaultdict(int)
        self._local_lock = asyncio.Lock()

    async def check(self, *, ip: str, tenant_id: UUID, identity: str) -> None:
        window = int(time.time() // 60)
        identity_hash = hashlib.sha256(
            f"{tenant_id}:{identity.lower().strip()}".encode()
        ).hexdigest()
        keys = [
            f"rate:login:ip:{window}:{ip}",
            f"rate:login:tenant:{window}:{tenant_id}",
            f"rate:login:identity:{window}:{identity_hash}",
        ]
        limits = [
            self._settings.rate_limit_per_ip,
            self._settings.rate_limit_per_tenant,
            self._settings.rate_limit_per_identity,
        ]

        try:
            exceeded = int(
                await self._redis.eval(_INCREMENT_SCRIPT, len(keys), *keys, *limits)
            )
        except RedisError as exc:
            if self._settings.environment in {"development", "test"}:
                logger.warning("auth.rate_limit.redis_unavailable", error=str(exc))
                exceeded = await self._check_local(keys, limits)
            else:
                logger.error("auth.rate_limit.redis_unavailable", error=str(exc))
                raise RateLimitUnavailableError() from exc

        if exceeded:
            dimensions = ("ip", "tenant", "identity")
            logger.warning(
                "auth.rate_limit.exceeded",
                dimension=dimensions[exceeded - 1],
                tenant_id=str(tenant_id),
                ip=ip,
            )
            raise RateLimitExceededError(details={"retry_after_seconds": 60})

    async def _check_local(self, keys: list[str], limits: list[int]) -> int:
        async with self._local_lock:
            for index, (key, limit) in enumerate(zip(keys, limits, strict=True), start=1):
                if self._local_counts[key] >= limit:
                    return index
            for key in keys:
                self._local_counts[key] += 1
        return 0

    async def close(self) -> None:
        await self._redis.aclose()
