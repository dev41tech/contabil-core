"""Testes de integração do fluxo de autenticação."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Tenant, Usuario


@pytest.mark.asyncio
async def test_login_sucesso(client: AsyncClient, usuario: Usuario, tenant: Tenant):
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_segura_123",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "csrf_token" in body
    assert len(body["csrf_token"]) > 10

    # Cookies HttpOnly devem estar presentes
    assert "access_token" in response.cookies
    assert "refresh_token" in response.cookies
    assert "csrf_token" in response.cookies


@pytest.mark.asyncio
async def test_login_senha_errada(client: AsyncClient, usuario: Usuario, tenant: Tenant):
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_errada",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_login_usuario_inexistente(client: AsyncClient, tenant: Tenant):
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "naoexiste@email.com",
            "senha": "qualquer_senha_123",
        },
    )
    # Mesma resposta — não revela se o usuário existe
    assert response.status_code == 401
    assert response.json()["error"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_me_sem_autenticacao(client: AsyncClient):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_com_autenticacao(client: AsyncClient, usuario: Usuario, tenant: Tenant):
    # Login
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_segura_123",
        },
    )
    assert login.status_code == 200

    # /me
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200

    body = me.json()
    assert body["email"] == "contador@41contabil.com.br"
    assert body["role"] == "admin"
    assert body["tenant_id"] == str(tenant.id)


@pytest.mark.asyncio
async def test_logout_limpa_cookies(client: AsyncClient, usuario: Usuario, tenant: Tenant):
    # Login
    await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_segura_123",
        },
    )

    # Logout
    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 204

    # /me deve falhar agora
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 401


@pytest.mark.asyncio
async def test_refresh_emite_novos_tokens(client: AsyncClient, usuario: Usuario, tenant: Tenant):
    # Login
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_segura_123",
        },
    )
    csrf_original = login.json()["csrf_token"]

    # Refresh
    refresh = await client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 200

    novo_csrf = refresh.json()["csrf_token"]
    assert novo_csrf != csrf_original  # Novo CSRF emitido


# ── Troca e reset de senha


async def _login_com(client, tenant, email: str, senha: str) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": email, "senha": senha},
    )
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


@pytest.mark.asyncio
async def test_trocar_a_propria_senha_e_logar_com_a_nova(client, db, tenant, usuario):
    """O caminho feliz, ponta a ponta: a senha antiga deixa de servir."""
    csrf = await _login_com(client, tenant, usuario.email, "senha_segura_123")

    r = await client.post(
        "/api/v1/auth/senha",
        json={"senha_atual": "senha_segura_123", "nova_senha": "outra_senha_456"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 204, r.text

    antiga = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert antiga.status_code == 401

    await _login_com(client, tenant, usuario.email, "outra_senha_456")


@pytest.mark.asyncio
async def test_trocar_senha_derruba_as_sessoes(client, db, tenant, usuario):
    """É o motivo de existir: senha vazada, sessão do invasor tem de cair.

    Sem revogar, quem estava logado com a senha antiga continua renovando o
    acesso pelo refresh token — trocar a senha não expulsaria ninguém.
    """
    from sqlalchemy import select

    from src.db.models import RefreshToken

    csrf = await _login_com(client, tenant, usuario.email, "senha_segura_123")
    vivos = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.usuario_id == usuario.id,
                RefreshToken.revogado == False,  # noqa: E712
            )
        )
    ).scalars().all()
    assert vivos, "o login deveria ter criado um refresh token vivo"

    r = await client.post(
        "/api/v1/auth/senha",
        json={"senha_atual": "senha_segura_123", "nova_senha": "outra_senha_456"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 204

    ainda_vivos = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.usuario_id == usuario.id,
                RefreshToken.revogado == False,  # noqa: E712
            )
        )
    ).scalars().all()
    assert ainda_vivos == []


@pytest.mark.asyncio
async def test_trocar_senha_exige_a_senha_atual(client, db, tenant, usuario):
    """Sessão roubada não pode virar conta roubada."""
    csrf = await _login_com(client, tenant, usuario.email, "senha_segura_123")

    r = await client.post(
        "/api/v1/auth/senha",
        json={"senha_atual": "chute_errado_123", "nova_senha": "outra_senha_456"},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 401
    # E a senha continua valendo.
    await _login_com(client, tenant, usuario.email, "senha_segura_123")


@pytest.mark.asyncio
async def test_trocar_senha_recusa_repetir_a_atual(client, db, tenant, usuario):
    csrf = await _login_com(client, tenant, usuario.email, "senha_segura_123")

    r = await client.post(
        "/api/v1/auth/senha",
        json={"senha_atual": "senha_segura_123", "nova_senha": "senha_segura_123"},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_trocar_senha_sem_csrf_rejeita(client, tenant, usuario):
    await _login_com(client, tenant, usuario.email, "senha_segura_123")

    r = await client.post(
        "/api/v1/auth/senha",
        json={"senha_atual": "senha_segura_123", "nova_senha": "outra_senha_456"},
    )

    assert r.status_code == 403
