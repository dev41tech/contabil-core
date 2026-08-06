"""Testes de integração do CRUD de empresas."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from src.db.models import Empresa, Tenant, Usuario


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    """Helper: faz login e retorna csrf_token."""
    r = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": usuario.email,
            "senha": "senha_segura_123",
        },
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


@pytest.mark.asyncio
async def test_listar_empresas_sem_autenticacao(client: AsyncClient):
    r = await client.get("/api/v1/empresas")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_listar_empresas_autenticado(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get("/api/v1/empresas")
    assert r.status_code == 200

    body = r.json()
    assert body["total"] >= 1
    assert any(e["cnpj"] == empresa.cnpj for e in body["items"])


@pytest.mark.asyncio
async def test_listar_empresas_pagina_alem_do_default_nao_perde_nenhuma(
    client: AsyncClient, db, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Regressão: o seletor global de empresa buscava sem `page_size`, caía no
    default de 50 e as empresas além da 50ª (ordem alfabética) simplesmente não
    apareciam em nenhuma tela — nem erro, nem indício de que faltava algo.

    Cria empresas suficientes para passar da primeira página e confirma que
    dá para acessar todas paginando, e que `total` diz a verdade sobre quantas
    existem — é o dado que o front precisa para saber que tem mais para buscar.
    """
    for i in range(60):
        db.add(
            Empresa(
                tenant_id=tenant.id,
                razao_social=f"EMPRESA TESTE PAGINACAO {i:03d} LTDA",
                cnpj=f"00.000.{i:03d}/0001-00",
                regime_tributario="simples_nacional",
            )
        )
    await db.flush()

    await _login(client, tenant, usuario)

    r = await client.get("/api/v1/empresas")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 61  # as 60 criadas + a fixture `empresa`
    assert len(body["items"]) == 50, "default de page_size não deveria mudar"

    r2 = await client.get("/api/v1/empresas", params={"page": 2, "page_size": 50})
    pagina_dois = r2.json()["items"]
    assert len(pagina_dois) >= 11, "o resto das empresas precisa estar acessível na página seguinte"

    ids_pagina_um = {e["id"] for e in body["items"]}
    ids_pagina_dois = {e["id"] for e in pagina_dois}
    assert not (ids_pagina_um & ids_pagina_dois), "páginas não podem se sobrepor"


@pytest.mark.asyncio
async def test_listar_empresas_page_size_acima_do_limite_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get("/api/v1/empresas", params={"page_size": 500})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_listar_cnpj_invalidos(
    client: AsyncClient, db, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Empresas com CNPJ placeholder da migração precisam aparecer numa lista.

    Enquanto ficam invisíveis, a exportação de notas delas volta vazia e ninguém
    descobre por quê.
    """
    placeholder = Empresa(
        tenant_id=tenant.id,
        razao_social="AJAF LOGISTICA LTDA",
        cnpj="01.003.007/0001-01",  # padrão gerado pelo scripts/import_mrcont.py
        regime_tributario="lucro_presumido",
    )
    db.add(placeholder)
    await db.flush()

    await _login(client, tenant, usuario)
    r = await client.get("/api/v1/empresas/cnpj-invalidos")
    assert r.status_code == 200

    body = r.json()
    cnpjs = [e["cnpj"] for e in body["items"]]
    assert "01.003.007/0001-01" in cnpjs
    assert empresa.cnpj not in cnpjs, "empresa com CNPJ válido não deveria estar na lista"
    assert body["total"] == len(body["items"])
    assert body["total_empresas"] >= body["total"]


@pytest.mark.asyncio
async def test_listar_cnpj_invalidos_exige_autenticacao(client: AsyncClient):
    r = await client.get("/api/v1/empresas/cnpj-invalidos")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_criar_empresa_com_cnpj_invalido_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario
):
    """O cadastro passou a barrar na entrada o que gerou as 72 empresas quebradas."""
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Empresa CNPJ Falso LTDA",
            "cnpj": "01.003.007/0001-01",
            "regime_tributario": "lucro_presumido",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_criar_empresa(client: AsyncClient, tenant: Tenant, usuario: Usuario):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Nova Empresa Teste LTDA",
            "cnpj": "98.765.432/0001-98",
            "regime_tributario": "lucro_presumido",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["razao_social"] == "Nova Empresa Teste LTDA"
    assert body["cnpj"] == "98.765.432/0001-98"
    assert "id" in body


@pytest.mark.asyncio
async def test_criar_empresa_cnpj_duplicado(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Outra Empresa LTDA",
            "cnpj": empresa.cnpj,  # CNPJ já existente
            "regime_tributario": "simples_nacional",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "CONFLICT"


@pytest.mark.asyncio
async def test_erro_de_cnpj_duplicado_nomeia_a_empresa_existente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Quem esbarra no duplicado precisa saber QUAL empresa já tem aquele CNPJ.

    O caso real é a mesma empresa já cadastrada com outra grafia da razão social
    — repetir só o CNPJ que a pessoa acabou de digitar não ajuda a perceber isso.
    """
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "DECATEC COMERCIO E SERVICOS LTDA",  # mesma empresa, nome completo
            "cnpj": empresa.cnpj,
            "regime_tributario": "simples_nacional",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409

    body = r.json()
    assert empresa.razao_social in body["message"], "a mensagem precisa nomear a empresa existente"
    assert body["details"]["empresa_id"] == str(empresa.id)
    assert body["details"]["razao_social"] == empresa.razao_social


@pytest.mark.asyncio
async def test_obter_empresa_existente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.get(f"/api/v1/empresas/{empresa.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(empresa.id)


@pytest.mark.asyncio
async def test_obter_empresa_inexistente(
    client: AsyncClient, tenant: Tenant, usuario: Usuario
):
    import uuid
    await _login(client, tenant, usuario)
    r = await client.get(f"/api/v1/empresas/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"] == "EMPRESA_NAO_ENCONTRADA"


@pytest.mark.asyncio
async def test_atualizar_empresa(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.patch(
        f"/api/v1/empresas/{empresa.id}",
        json={"razao_social": "DECATEC LTDA ATUALIZADO"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["razao_social"] == "DECATEC LTDA ATUALIZADO"


@pytest.mark.asyncio
async def test_criar_empresa_sem_csrf_falha(
    client: AsyncClient, tenant: Tenant, usuario: Usuario
):
    await _login(client, tenant, usuario)
    r = await client.post(
        "/api/v1/empresas",
        json={
            "razao_social": "Empresa Sem CSRF",
            "cnpj": "11.111.111/0001-91",
            "regime_tributario": "lucro_real",
        },
        # sem X-CSRF-Token
    )
    assert r.status_code == 403
