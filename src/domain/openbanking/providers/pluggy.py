"""Provedor Pluggy — Open Finance Brasil.

Documentação: https://docs.pluggy.ai/

Requer:
  PLUGGY_CLIENT_ID  e  PLUGGY_CLIENT_SECRET  no .env

Fluxo de autenticação:
  1. POST /auth → apiKey  (válido por ~2h, deve ser renovado)
  2. POST /connect_token  → accessToken  (para o widget)
  3. Usuário autentica no widget → retorna item_id via postMessage
  4. GET /items/{item_id} → status e info do item
  5. GET /accounts?itemId=... → contas
  6. GET /transactions?accountId=...&from=...&to=... → transações
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from src.domain.openbanking.providers.base import (
    ContaInfo,
    IOpenBankingProvider,
    TransacaoInfo,
)

logger = logging.getLogger(__name__)

_BASE = "https://api.pluggy.ai"
_API_KEY_TTL = timedelta(hours=1, minutes=50)  # renova um pouco antes dos 2h


class PluggyProvider(IOpenBankingProvider):
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_key: str | None = None
        self._api_key_expires: datetime | None = None

    # ── auth ─────────────────────────────────────────────────────────────────

    async def _get_api_key(self) -> str:
        now = datetime.now(UTC)
        if self._api_key and self._api_key_expires and now < self._api_key_expires:
            return self._api_key

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{_BASE}/auth",
                json={"clientId": self._client_id, "clientSecret": self._client_secret},
            )
            r.raise_for_status()
            data = r.json()

        self._api_key = data["apiKey"]
        self._api_key_expires = now + _API_KEY_TTL
        return self._api_key

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"X-API-KEY": api_key}

    # ── interface ─────────────────────────────────────────────────────────────

    async def criar_connect_token(
        self,
        item_id: str | None = None,
        client_user_id: str | None = None,
    ) -> str:
        api_key = await self._get_api_key()
        body: dict = {}
        if item_id:
            body["itemId"] = item_id
        if client_user_id:
            body["clientUserId"] = client_user_id

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{_BASE}/connect_token",
                json=body,
                headers=self._headers(api_key),
            )
            r.raise_for_status()

        return r.json()["accessToken"]

    async def validar_item(self, item_id: str, client_user_id: str) -> bool:
        api_key = await self._get_api_key()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_BASE}/items/{item_id}",
                headers=self._headers(api_key),
            )
            r.raise_for_status()
        vinculo = r.json().get("clientUserId")
        return isinstance(vinculo, str) and hmac.compare_digest(vinculo, client_user_id)

    async def obter_contas(self, item_id: str) -> list[ContaInfo]:
        api_key = await self._get_api_key()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_BASE}/accounts",
                params={"itemId": item_id},
                headers=self._headers(api_key),
            )
            r.raise_for_status()

        results = r.json().get("results", [])
        contas = []
        for acc in results:
            tipo = acc.get("type", "CHECKING")
            if tipo == "CREDIT":  # cartão — ignorar aqui
                continue
            bank_data = acc.get("bankData", {})
            contas.append(
                ContaInfo(
                    account_id=acc["id"],
                    banco_sigla=_sigla_banco(acc.get("institution", {}).get("primaryColor", ""), acc),
                    instituicao_nome=acc.get("institution", {}).get("name", "Banco"),
                    instituicao_codigo=acc.get("institution", {}).get("primaryColor"),  # campo reutilizado como código
                    agencia=bank_data.get("transferNumber", "").split("/")[0] if "/" in bank_data.get("transferNumber", "") else None,
                    numero=bank_data.get("transferNumber", "").split("/")[-1] if bank_data.get("transferNumber") else acc.get("number"),
                    tipo=tipo,
                    saldo=acc.get("balance"),
                )
            )
        return contas

    async def obter_transacoes(
        self, account_id: str, data_inicio: date, data_fim: date
    ) -> list[TransacaoInfo]:
        api_key = await self._get_api_key()
        results: list[dict] = []
        page = 1

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                r = await client.get(
                    f"{_BASE}/transactions",
                    params={
                        "accountId": account_id,
                        "from": str(data_inicio),
                        "to": str(data_fim),
                        "page": page,
                        "pageSize": 500,
                    },
                    headers=self._headers(api_key),
                )
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("results", []))
                if page >= data.get("totalPages", 1):
                    break
                page += 1

        return [_map_transacao(t) for t in results]

    async def obter_nome_instituicao(self, item_id: str) -> str:
        api_key = await self._get_api_key()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_BASE}/items/{item_id}",
                headers=self._headers(api_key),
            )
            r.raise_for_status()
        data = r.json()
        return data.get("connector", {}).get("name", "Banco")


# ── helpers ───────────────────────────────────────────────────────────────────

def _map_transacao(t: dict) -> TransacaoInfo:
    amount = float(t.get("amount", 0))
    tipo = t.get("type", "DEBIT")
    dc = "C" if tipo == "CREDIT" or amount > 0 else "D"
    return TransacaoInfo(
        id_externo=t["id"],
        data=date.fromisoformat(t["date"][:10]),
        valor=abs(amount),
        dc=dc,
        descricao=t.get("description") or t.get("descriptionRaw") or "Sem descrição",
        categoria=t.get("category"),
    )


def _sigla_banco(cor: str, acc: dict) -> str:
    """Heurística para sigla do banco a partir dos dados do item Pluggy."""
    nome: str = acc.get("institution", {}).get("name", "BANCO")
    # Pega as primeiras palavras significativas
    palavras = [w for w in nome.upper().split() if w not in ("BANCO", "S.A.", "S/A", "LTDA", "S.A")]
    return palavras[0][:10] if palavras else nome[:10]
