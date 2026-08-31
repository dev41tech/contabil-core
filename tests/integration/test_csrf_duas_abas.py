"""Duas abas da MESMA conta, compartilhando o mesmo pote de cookies do navegador.

O relato que originou estes testes: "não consigo usar a mesma conta em duas abas
diferentes", com erro de CSRF logo na primeira gravação. A causa não está no
CSRF — está na rotação do refresh token, e o erro de CSRF é a consequência
visível dela.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from src.db.models import Tenant, Usuario


async def _login(client: AsyncClient, tenant: Tenant) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_segura_123",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


@pytest.mark.asyncio
async def test_a_aba_que_perde_a_corrida_do_refresh_leva_401(client: AsyncClient,
                                                             usuario: Usuario,
                                                             tenant: Tenant):
    """Duas abas renovando a sessão: a segunda manda um token já consumido.

    `AuthService.refresh` revoga o token usado no mesmo UPDATE condicional que o
    consome, e não existe janela de tolerância. Quando as duas abas renovam
    quase juntas — no boot da segunda, ou quando o access token expira para as
    duas ao mesmo tempo — uma ganha e a outra recebe 401.

    Este teste documenta o comportamento atual. Ele NÃO é o comportamento
    desejado: é a causa raiz do relato de CSRF em duas abas.
    """
    await _login(client, tenant)
    token_da_aba_lenta = client.cookies.get("refresh_token")
    assert token_da_aba_lenta

    ganhadora = await client.post("/api/v1/auth/refresh")
    assert ganhadora.status_code == 200

    perdedora = await client.post(
        "/api/v1/auth/refresh", cookies={"refresh_token": token_da_aba_lenta}
    )
    assert perdedora.status_code == 401
    assert "já utilizado" in perdedora.json()["message"]


@pytest.mark.asyncio
async def test_a_sessao_continua_viva_para_a_aba_que_perdeu(client: AsyncClient,
                                                            usuario: Usuario,
                                                            tenant: Tenant):
    """O 401 acima NÃO significa sessão morta — e é isso que resolve o caso.

    A aba que perdeu a corrida não precisa de sessão nova: a aba que ganhou já
    deixou cookies válidos no pote, que é do navegador inteiro. Se o frontend
    repetir a requisição original em vez de tratar o 401 como fim de sessão, ela
    passa.

    Enquanto ele derruba a sessão, o cookie de CSRF é apagado junto — e a outra
    aba passa a receber "CSRF token inválido" por cookie AUSENTE, que é o
    sintoma relatado.
    """
    csrf = await _login(client, tenant)
    token_da_aba_lenta = client.cookies.get("refresh_token")

    await client.post("/api/v1/auth/refresh")
    perdedora = await client.post(
        "/api/v1/auth/refresh", cookies={"refresh_token": token_da_aba_lenta}
    )
    assert perdedora.status_code == 401

    # Mesmo depois do 401, a sessão do navegador está viva e o CSRF é o mesmo
    # de antes — porque o refresh passou a preservá-lo (ver `_set_auth_cookies`).
    assert client.cookies.get("csrf_token") == csrf

    gravacao = await client.post(
        "/api/v1/auth/senha",
        json={"senha_atual": "senha_segura_123", "senha_nova": "outra_senha_123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert gravacao.status_code != 403, gravacao.text
