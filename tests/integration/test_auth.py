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

    # O CSRF ATRAVESSA a renovação, e é de propósito. A asserção aqui era a
    # inversa — `novo_csrf != csrf_original`, "Novo CSRF emitido" — e é essa
    # rotação que derrubava a segunda aba: ver o teste de duas abas abaixo.
    novo_csrf = refresh.json()["csrf_token"]
    assert novo_csrf == csrf_original


@pytest.mark.asyncio
async def test_refresh_preserva_o_csrf_da_outra_aba(client: AsyncClient, usuario: Usuario,
                                                    tenant: Tenant):
    """Duas abas abertas, uma renova, a outra grava: não pode dar CSRF inválido.

    O cookie de CSRF é um só para o navegador; o token que o frontend guarda é
    um por aba, porque vem no corpo do login/refresh. Enquanto o refresh emitia
    um valor novo, a aba que não tinha feito aquele refresh seguia mandando o
    anterior no header e levava 403 na primeira mutação — a cada 15 minutos.

    Aqui o cliente é o navegador (cookie compartilhado) e `csrf_aba_2` é o que a
    segunda aba tem em memória, de antes da renovação.
    """
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_segura_123",
        },
    )
    csrf_aba_2 = login.json()["csrf_token"]

    # A aba 1 renova a sessão. O cookie do navegador inteiro é reescrito.
    assert (await client.post("/api/v1/auth/refresh")).status_code == 200

    # A aba 2 grava com o token que ela tinha desde o login.
    resposta = await client.post(
        "/api/v1/auth/senha",
        json={"senha_atual": "senha_segura_123", "senha_nova": "outra_senha_123"},
        headers={"X-CSRF-Token": csrf_aba_2},
    )
    assert resposta.status_code != 403, resposta.text


@pytest.mark.asyncio
async def test_login_emite_csrf_novo(client: AsyncClient, usuario: Usuario, tenant: Tenant):
    """O refresh preserva; o LOGIN não pode preservar.

    Um cookie plantado antes da autenticação não pode sobreviver a ela — é a
    única parte da rotação que protegia alguma coisa, e ela fica.
    """
    dados = {
        "tenant_id": str(tenant.id),
        "email": "contador@41contabil.com.br",
        "senha": "senha_segura_123",
    }
    primeiro = await client.post("/api/v1/auth/login", json=dados)
    segundo = await client.post("/api/v1/auth/login", json=dados)

    assert primeiro.json()["csrf_token"] != segundo.json()["csrf_token"]


@pytest.mark.asyncio
async def test_csrf_dura_mais_que_o_access_token(client: AsyncClient, usuario: Usuario,
                                                 tenant: Tenant):
    """Aba parada não pode perder o cookie e receber 403 onde cabia 401.

    Com a validade colada nos 15 minutos do access token, a aba ociosa ficava
    sem cookie de CSRF e a próxima mutação virava "CSRF token inválido" — erro
    que manda o usuário procurar problema onde não há, em vez do 401 que
    dispararia a renovação.
    """
    from src.core.config import get_settings

    resposta = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": "contador@41contabil.com.br",
            "senha": "senha_segura_123",
        },
    )
    ajustes = get_settings()
    bruto = next(v for v in resposta.headers.get_list("set-cookie")
                 if v.startswith("csrf_token="))
    idade = int(bruto.split("Max-Age=")[1].split(";")[0])

    assert idade > ajustes.access_token_ttl_minutes * 60
    assert idade == ajustes.refresh_token_ttl_days * 86400


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
