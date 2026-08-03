"""Testes de integração — Open Banking.

Sem credenciais Pluggy o serviço cai no `MockProvider`, que é determinístico por
`item_id` — é o que torna estes testes estáveis. O que realmente precisa estar
certo é a deduplicação: sincronizar duas vezes o mesmo período não pode duplicar
transação no extrato, senão a conciliação passa a ver movimento que não existiu.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Empresa, Tenant, Usuario

_ITEM_ID = "item_teste_openbanking_001"


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": "senha_segura_123"},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, sufixo: str = "") -> str:
    return f"/api/v1/empresas/{empresa_id}/openbanking{sufixo}"


async def _conectar(client: AsyncClient, empresa: Empresa, csrf: str, item_id=_ITEM_ID) -> dict:
    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={"item_id": item_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── acesso ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listar_sem_autenticacao_rejeita(client: AsyncClient, empresa: Empresa):
    assert (await client.get(_url(empresa.id, "/conexoes"))).status_code == 401


@pytest.mark.asyncio
async def test_conectar_sem_csrf_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.post(_url(empresa.id, "/conexoes"), json={"item_id": _ITEM_ID})
    assert r.status_code == 403


# ── connect token ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_token_indica_mock_sem_credenciais(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Sem PLUGGY_* configurado o front precisa saber que está em modo mock."""
    csrf = await _login(client, tenant, usuario)
    r = await client.post(_url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf})

    assert r.status_code == 200
    body = r.json()
    assert body["provedor"] == "mock"
    assert body["mock_mode"] is True
    assert body["access_token"].startswith("mock_")


# ── salvar conexão ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conectar_preenche_dados_do_banco(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)

    assert conexao["item_id"] == _ITEM_ID
    assert conexao["provedor"] == "mock"
    assert conexao["status"] == "ativa"
    assert conexao["banco_sigla"]
    assert conexao["instituicao_nome"]
    assert conexao["total_transacoes_sync"] == 0


@pytest.mark.asyncio
async def test_conectar_o_mesmo_item_duas_vezes_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Reconectar a mesma conta deve mandar sincronizar, não criar conexão paralela."""
    csrf = await _login(client, tenant, usuario)
    await _conectar(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={"item_id": _ITEM_ID},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_item_id_vazio_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id, "/conexoes"), json={"item_id": ""}, headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_listar_conexoes(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    assert (await client.get(_url(empresa.id, "/conexoes"))).json()["total"] == 0

    await _conectar(client, empresa, csrf)
    await _conectar(client, empresa, csrf, item_id="item_teste_openbanking_002")

    body = (await client.get(_url(empresa.id, "/conexoes"))).json()
    assert body["total"] == 2


# ── sincronização ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sincronizar_importa_e_cria_agencia(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id, f"/conexoes/{conexao['id']}/sincronizar"),
        json={"dias": 30},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "concluido"
    assert body["importadas"] > 0
    assert body["duplicadas"] == 0
    assert body["erros"] == 0

    # As transações precisam chegar ao extrato, sob uma agência criada na hora
    extrato = (await client.get(f"/api/v1/empresas/{empresa.id}/extrato")).json()
    assert extrato["total"] == body["importadas"]

    agencias = (await client.get(f"/api/v1/empresas/{empresa.id}/agencias")).json()
    assert agencias["total"] == 1


@pytest.mark.asyncio
async def test_sincronizar_duas_vezes_nao_duplica(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """O ponto da dedup: a 2ª sincronização do mesmo período não traz nada novo."""
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)
    url = _url(empresa.id, f"/conexoes/{conexao['id']}/sincronizar")

    primeira = (
        await client.post(url, json={"dias": 30}, headers={"X-CSRF-Token": csrf})
    ).json()
    segunda = (
        await client.post(url, json={"dias": 30}, headers={"X-CSRF-Token": csrf})
    ).json()

    assert segunda["importadas"] == 0
    assert segunda["duplicadas"] == primeira["importadas"]

    extrato = (await client.get(f"/api/v1/empresas/{empresa.id}/extrato")).json()
    assert extrato["total"] == primeira["importadas"]


@pytest.mark.asyncio
async def test_sincronizar_atualiza_estado_da_conexao(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)

    sync = (
        await client.post(
            _url(empresa.id, f"/conexoes/{conexao['id']}/sincronizar"),
            json={"dias": 30},
            headers={"X-CSRF-Token": csrf},
        )
    ).json()

    atualizada = (await client.get(_url(empresa.id, "/conexoes"))).json()["items"][0]
    assert atualizada["last_sync_at"] is not None
    assert atualizada["next_sync_at"] is not None
    assert atualizada["agencia_id"] is not None
    assert atualizada["total_transacoes_sync"] == sync["importadas"]
    assert atualizada["erro_msg"] is None


@pytest.mark.asyncio
async def test_dias_fora_do_intervalo_permitido_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)
    url = _url(empresa.id, f"/conexoes/{conexao['id']}/sincronizar")

    assert (await client.post(url, json={"dias": 0}, headers={"X-CSRF-Token": csrf})).status_code == 422
    assert (await client.post(url, json={"dias": 366}, headers={"X-CSRF-Token": csrf})).status_code == 422


@pytest.mark.asyncio
async def test_sincronizar_conexao_inexistente_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id, f"/conexoes/{uuid.uuid4()}/sincronizar"),
        json={"dias": 30},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


# ── reconectar e remover ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconectar_gera_token(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)

    r = await client.post(
        _url(empresa.id, f"/conexoes/{conexao['id']}/reconectar"), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 200
    assert r.json()["access_token"].startswith("mock_")


@pytest.mark.asyncio
async def test_remover_conexao_e_soft_delete(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)

    r = await client.delete(
        _url(empresa.id, f"/conexoes/{conexao['id']}"), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 204
    assert (await client.get(_url(empresa.id, "/conexoes"))).json()["total"] == 0


@pytest.mark.asyncio
async def test_conexao_removida_nao_sincroniza_mais(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)
    await client.delete(
        _url(empresa.id, f"/conexoes/{conexao['id']}"), headers={"X-CSRF-Token": csrf}
    )

    r = await client.post(
        _url(empresa.id, f"/conexoes/{conexao['id']}/sincronizar"),
        json={"dias": 30},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404
