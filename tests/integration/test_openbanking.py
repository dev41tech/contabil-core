"""Testes de integração — Open Banking.

Em desenvolvimento, sem credenciais Pluggy, o serviço usa o `MockProvider`, que
é determinístico por `item_id`. Em outros ambientes, a ausência de credenciais
deve falhar explicitamente.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import PluggyUnavailableError
from src.db.models import Empresa, Tenant, Usuario
from src.domain.openbanking import service as openbanking_service
from src.domain.openbanking.providers.base import ContaInfo
from src.domain.openbanking.providers.mock import MockProvider

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
    token = await client.post(
        _url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf}
    )
    assert token.status_code == 200, token.text
    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={
            "item_id": item_id,
            "connection_session": token.json()["connection_session"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    assert r.json()["total"] >= 1
    return r.json()["items"][0]


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
    assert body["connection_session"]


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

    token = await client.post(
        _url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf}
    )

    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={
            "item_id": _ITEM_ID,
            "connection_session": token.json()["connection_session"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_item_id_vazio_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    token = await client.post(
        _url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf}
    )
    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={
            "item_id": "",
            "connection_session": token.json()["connection_session"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_callback_exige_sessao_emitida_pelo_servidor(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={"item_id": _ITEM_ID},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_sessao_de_uma_empresa_nao_pode_anexar_item_em_outra(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    outra = Empresa(
        tenant_id=tenant.id,
        razao_social="OUTRA OPEN BANKING LTDA",
        cnpj="48.309.387/0001-04",
        regime_tributario="lucro_real",
    )
    db.add(outra)
    await db.flush()
    csrf = await _login(client, tenant, usuario)
    token = await client.post(
        _url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf}
    )

    r = await client.post(
        _url(outra.id, "/conexoes"),
        json={
            "item_id": _ITEM_ID,
            "connection_session": token.json()["connection_session"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_callback_rejeita_item_sem_vinculo_no_provedor(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    async def item_de_outra_sessao(
        self: MockProvider, item_id: str, client_user_id: str
    ) -> bool:
        return False

    monkeypatch.setattr(MockProvider, "validar_item", item_de_outra_sessao)
    csrf = await _login(client, tenant, usuario)
    token = await client.post(
        _url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf}
    )
    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={
            "item_id": _ITEM_ID,
            "connection_session": token.json()["connection_session"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_salvar_conexao_representa_todas_as_contas_do_item(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    original = MockProvider.obter_contas

    async def duas_contas(self: MockProvider, item_id: str) -> list[ContaInfo]:
        contas = await original(self, item_id)
        primeira = contas[0]
        return [
            primeira,
            ContaInfo(
                account_id=f"{primeira.account_id}_poupanca",
                banco_sigla=primeira.banco_sigla,
                instituicao_nome=primeira.instituicao_nome,
                instituicao_codigo=primeira.instituicao_codigo,
                agencia=primeira.agencia,
                numero=f"{primeira.numero}-P",
                tipo="SAVINGS",
                saldo=Decimal("1000.00"),
            ),
        ]

    monkeypatch.setattr(MockProvider, "obter_contas", duas_contas)
    csrf = await _login(client, tenant, usuario)
    token = await client.post(
        _url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf}
    )
    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={
            "item_id": _ITEM_ID,
            "connection_session": token.json()["connection_session"],
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 201, r.text
    assert r.json()["total"] == 2
    assert len({item["conta_numero"] for item in r.json()["items"]}) == 2


@pytest.mark.asyncio
async def test_erro_do_provedor_ao_validar_nao_vaza_detalhe_interno(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    segredo = "senha-interna-do-provedor"

    async def falhar(self: MockProvider, item_id: str):
        raise RuntimeError(segredo)

    monkeypatch.setattr(MockProvider, "obter_contas", falhar)
    csrf = await _login(client, tenant, usuario)
    token = await client.post(
        _url(empresa.id, "/connect-token"), headers={"X-CSRF-Token": csrf}
    )
    r = await client.post(
        _url(empresa.id, "/conexoes"),
        json={
            "item_id": _ITEM_ID,
            "connection_session": token.json()["connection_session"],
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 422
    assert segredo not in r.text
    assert "validar a conexão bancária" in r.text


def test_mock_nao_e_permitido_fora_de_desenvolvimento(
    monkeypatch: pytest.MonkeyPatch,
):
    class SettingsSemPluggy:
        pluggy_enabled = False
        is_development = False

    monkeypatch.setattr(openbanking_service, "get_settings", lambda: SettingsSemPluggy())
    with pytest.raises(PluggyUnavailableError):
        openbanking_service._get_provider()


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
async def test_erro_do_provedor_ao_sincronizar_nao_vaza_no_cliente_nem_na_conexao(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
):
    segredo = "token-secreto-na-stack-do-provedor"
    csrf = await _login(client, tenant, usuario)
    conexao = await _conectar(client, empresa, csrf)

    async def falhar(self: MockProvider, account_id, data_inicio, data_fim):
        raise RuntimeError(segredo)

    monkeypatch.setattr(MockProvider, "obter_transacoes", falhar)
    r = await client.post(
        _url(empresa.id, f"/conexoes/{conexao['id']}/sincronizar"),
        json={"dias": 30},
        headers={"X-CSRF-Token": csrf},
    )

    assert r.status_code == 422
    assert segredo not in r.text
    assert "sincronizar as transações bancárias" in r.text

    armazenada = (await client.get(_url(empresa.id, "/conexoes"))).json()["items"][0]
    assert segredo not in armazenada["erro_msg"]
    assert armazenada["erro_msg"] == "Falha temporária ao sincronizar transações."


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
