"""Testes de integração — Permissões por empresa.

É o controle de acesso do sistema: a tabela `permissoes` é o que decide se um
contador enxerga os dados de uma empresa-cliente. Uma regressão aqui não quebra
nada visivelmente — ela vaza dados de um cliente para outro. Daí a cobertura
insistir tanto no que **deve ser negado** quanto no que deve funcionar.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import hash_password
from src.db.models import Empresa, Permissao, Tenant, Usuario

_SENHA = "senha_segura_123"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def contador(db: AsyncSession, tenant: Tenant) -> Usuario:
    """Usuário não-admin — só enxerga empresas às quais recebeu permissão."""
    u = Usuario(
        tenant_id=tenant.id,
        email="contador.junior@41contabil.com.br",
        nome="Ana Contadora",
        senha_hash=hash_password(_SENHA),
        role="contador",
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def outra_empresa(db: AsyncSession, tenant: Tenant) -> Empresa:
    e = Empresa(
        tenant_id=tenant.id,
        razao_social="SEGUNDA EMPRESA LTDA",
        cnpj="52.540.787/0001-88",
        regime_tributario="lucro_real",
    )
    db.add(e)
    await db.flush()
    return e


@pytest_asyncio.fixture
async def usuario_de_outro_escritorio(db: AsyncSession) -> Usuario:
    """Usuário de outro tenant — nunca deve receber permissão nas empresas deste."""
    outro_tenant = Tenant(nome="Escritório Rival", cnpj="12.810.326/0001-63")
    db.add(outro_tenant)
    await db.flush()

    u = Usuario(
        tenant_id=outro_tenant.id,
        email="intruso@rival.com.br",
        nome="Contador Rival",
        senha_hash=hash_password(_SENHA),
        role="contador",
    )
    db.add(u)
    await db.flush()
    return u


async def _login(client: AsyncClient, tenant: Tenant, usuario: Usuario) -> str:
    r = await client.post(
        "/api/v1/auth/login",
        json={"tenant_id": str(tenant.id), "email": usuario.email, "senha": _SENHA},
    )
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _url(empresa_id, sufixo: str = "") -> str:
    return f"/api/v1/empresas/{empresa_id}/permissoes{sufixo}"


# ── quem pode gerenciar permissões ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listar_sem_autenticacao_rejeita(client: AsyncClient, empresa: Empresa):
    r = await client.get(_url(empresa.id))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_contador_nao_gerencia_permissoes(
    client: AsyncClient, tenant: Tenant, contador: Usuario, empresa: Empresa
):
    """Só admin administra acesso — senão qualquer contador se autoconcede empresas."""
    await _login(client, tenant, contador)
    r = await client.get(_url(empresa.id))
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_contador_nao_concede_permissao(
    client: AsyncClient, tenant: Tenant, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, contador)
    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "*"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_conceder_sem_csrf_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "*"},
    )
    assert r.status_code == 403


# ── conceder / listar / atualizar / revogar ───────────────────────────────────


@pytest.mark.asyncio
async def test_conceder_e_listar(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)

    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "extrato,notas"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["usuario_email"] == contador.email
    assert body["usuario_role"] == "contador"
    assert body["modulos"] == "extrato,notas"

    r = await client.get(_url(empresa.id))
    assert r.status_code == 200
    listagem = r.json()
    assert listagem["total"] == 1
    assert listagem["items"][0]["usuario_id"] == str(contador.id)


@pytest.mark.asyncio
async def test_conceder_duplicado_retorna_409(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    payload = {"usuario_id": str(contador.id), "modulos": "*"}

    r1 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r1.status_code == 201

    r2 = await client.post(_url(empresa.id), json=payload, headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 409
    assert r2.json()["error"] == "CONFLICT"


@pytest.mark.asyncio
async def test_conceder_a_usuario_de_outro_escritorio_rejeita(
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    empresa: Empresa,
    usuario_de_outro_escritorio: Usuario,
):
    """O isolamento entre escritórios: não dá para conceder acesso a quem é de outro tenant."""
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(usuario_de_outro_escritorio.id), "modulos": "*"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_conceder_a_usuario_inexistente_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(uuid.uuid4()), "modulos": "*"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_atualizar_modulos(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "extrato"},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.patch(
        _url(empresa.id, f"/{contador.id}"),
        json={"modulos": "notas,regras"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["modulos"] == "notas,regras"


@pytest.mark.asyncio
async def test_atualizar_permissao_inexistente_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.patch(
        _url(empresa.id, f"/{contador.id}"),
        json={"modulos": "*"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_revogar_remove_da_listagem(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "*"},
        headers={"X-CSRF-Token": csrf},
    )

    r = await client.delete(
        _url(empresa.id, f"/{contador.id}"), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 204

    listagem = (await client.get(_url(empresa.id))).json()
    assert listagem["total"] == 0


@pytest.mark.asyncio
async def test_revogar_permissao_inexistente_retorna_404(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.delete(
        _url(empresa.id, f"/{contador.id}"), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 404


# ── escopo por empresa ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permissao_nao_vaza_entre_empresas(
    client: AsyncClient,
    tenant: Tenant,
    usuario: Usuario,
    contador: Usuario,
    empresa: Empresa,
    outra_empresa: Empresa,
):
    """Permissão é por empresa: conceder numa não pode aparecer na outra."""
    csrf = await _login(client, tenant, usuario)
    await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "*"},
        headers={"X-CSRF-Token": csrf},
    )

    assert (await client.get(_url(empresa.id))).json()["total"] == 1
    assert (await client.get(_url(outra_empresa.id))).json()["total"] == 0

    # E revogar na outra empresa não encontra nada para revogar
    r = await client.delete(
        _url(outra_empresa.id, f"/{contador.id}"), headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 404


# ── efeito real da permissão no acesso aos dados ──────────────────────────────


@pytest.mark.asyncio
async def test_contador_sem_permissao_nao_acessa_dados_da_empresa(
    client: AsyncClient, tenant: Tenant, contador: Usuario, empresa: Empresa
):
    """O ponto que importa: sem linha em `permissoes`, os dados da empresa ficam fora de alcance."""
    await _login(client, tenant, contador)
    r = await client.get(f"/api/v1/empresas/{empresa.id}/plano-contas")
    assert r.status_code == 403
    assert r.json()["error"] == "TENANT_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_contador_com_permissao_acessa_dados_da_empresa(
    client: AsyncClient, db: AsyncSession, tenant: Tenant, contador: Usuario, empresa: Empresa
):
    db.add(Permissao(usuario_id=contador.id, empresa_id=empresa.id, modulos="*"))
    await db.flush()

    await _login(client, tenant, contador)
    r = await client.get(f"/api/v1/empresas/{empresa.id}/plano-contas")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_permissao_numa_empresa_nao_abre_a_outra(
    client: AsyncClient,
    db: AsyncSession,
    tenant: Tenant,
    contador: Usuario,
    empresa: Empresa,
    outra_empresa: Empresa,
):
    db.add(Permissao(usuario_id=contador.id, empresa_id=empresa.id, modulos="*"))
    await db.flush()

    await _login(client, tenant, contador)
    assert (await client.get(f"/api/v1/empresas/{empresa.id}/plano-contas")).status_code == 200

    r = await client.get(f"/api/v1/empresas/{outra_empresa.id}/plano-contas")
    assert r.status_code == 403
    assert r.json()["error"] == "TENANT_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_admin_acessa_sem_linha_em_permissoes(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, empresa: Empresa
):
    """Admin passa por cima de `permissoes` — comportamento intencional, fixado em teste."""
    await _login(client, tenant, usuario)
    r = await client.get(f"/api/v1/empresas/{empresa.id}/plano-contas")
    assert r.status_code == 200


# ── validação de módulos ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modulo_invalido_rejeita(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "extrato,modulo_inventado"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_modulos_normalizados_e_ordenados(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    """Entrada bagunçada vira string canônica — senão o mesmo acesso grava de N formas."""
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": " NOTAS , extrato ,notas"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    assert r.json()["modulos"] == "extrato,notas"


@pytest.mark.asyncio
async def test_asterisco_aceito_como_acesso_total(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    csrf = await _login(client, tenant, usuario)
    r = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "*"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    assert r.json()["modulos"] == "*"


# ── Rota × módulo concedível


def test_todo_modulo_de_rota_e_concedivel_ou_admin():
    """Módulo de rota tem de cair de um dos dois lados — nunca em nenhum.

    O módulo da requisição é o primeiro segmento depois de
    `/empresas/{empresa_id}/` (`get_company_context`). Se ele não estiver em
    `MODULOS_VALIDOS`, a validação recusa concedê-lo e o administrador é
    empurrado para o `"*"` — dar tudo — quando queria dar um módulo só.

    Foi assim que `concilpro` e `aplicacoes_financeiras` passaram a existir
    como rota sem nunca poderem ser concedidos isoladamente. Este teste é o que
    impede a próxima rota de nascer com o mesmo buraco.
    """
    import re

    from src.api.app import create_app
    from src.schemas.permissoes import MODULOS_SOMENTE_ADMIN, MODULOS_VALIDOS

    padrao = re.compile(r"/empresas/\{empresa_id\}/([^/]+)")
    modulos = {
        m.group(1).replace("-", "_")
        for rota in create_app().routes
        if (m := padrao.search(getattr(rota, "path", "")))
    }
    assert modulos, "nenhuma rota de empresa encontrada — o padrão mudou?"

    orfaos = modulos - set(MODULOS_VALIDOS) - set(MODULOS_SOMENTE_ADMIN)
    assert not orfaos, (
        f"Módulos de rota que ninguém pode receber: {sorted(orfaos)}. "
        f"Inclua em MODULOS_VALIDOS (se o contador pode ter) ou em "
        f"MODULOS_SOMENTE_ADMIN (se a rota exige papel de admin)."
    )


def test_modulo_declarado_admin_tem_rota_que_exige_admin():
    """A lista de só-admin não pode virar depósito do que se quer esconder.

    Se um módulo está lá, alguma rota dele precisa mesmo exigir papel
    administrativo — senão a exclusão da lista concedível vira acesso negado
    sem motivo, e o contador fica sem um módulo que poderia ter.
    """
    from src.api.app import create_app
    from src.schemas.permissoes import MODULOS_SOMENTE_ADMIN

    def nomes_das_dependencias(dependant) -> set[str]:
        """Percorre a árvore de dependências da rota.

        Olhar só o corpo do endpoint não serve: em `permissoes` a exigência de
        admin mora numa dependência intermediária (`_svc`), e em `auditoria`
        vem de `get_admin_company_context`. A garantia está na árvore, não no
        texto da função.
        """
        nomes = {getattr(dependant.call, "__name__", "")}
        for sub in dependant.dependencies:
            nomes |= nomes_das_dependencias(sub)
        return nomes

    app = create_app()
    for modulo in MODULOS_SOMENTE_ADMIN:
        prefixos = {
            f"/empresas/{{empresa_id}}/{modulo}",
            f"/empresas/{{empresa_id}}/{modulo.replace('_', '-')}",
        }
        rotas = [
            r
            for r in app.routes
            if any(p in getattr(r, "path", "") for p in prefixos)
            and getattr(r, "dependant", None) is not None
        ]
        assert rotas, f"{modulo} está em MODULOS_SOMENTE_ADMIN e não tem rota"
        for rota in rotas:
            nomes = nomes_das_dependencias(rota.dependant)
            assert any("admin" in n for n in nomes), (
                f"{rota.path} está sob o módulo {modulo}, declarado só-admin, "
                f"mas nenhuma dependência exige papel administrativo: {sorted(nomes)}"
            )


@pytest.mark.asyncio
async def test_conceder_modulo_de_rota_que_nao_era_concedivel(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    """`concilpro` sozinho tem de ser concedível — sem obrigar o `"*"`.

    Antes desta correção a validação recusava, e quem quisesse dar ConcilPro a
    um contador precisava dar acesso total à empresa.
    """
    csrf = await _login(client, tenant, usuario)

    resposta = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "concilpro,aplicacoes_financeiras"},
        headers={"X-CSRF-Token": csrf},
    )

    assert resposta.status_code == 201, resposta.text
    assert resposta.json()["modulos"] == "aplicacoes_financeiras,concilpro"


@pytest.mark.asyncio
async def test_conceder_modulo_somente_admin_continua_recusado(
    client: AsyncClient, tenant: Tenant, usuario: Usuario, contador: Usuario, empresa: Empresa
):
    """`auditoria` não pode ser concedida: a rota exige admin de qualquer jeito.

    Aceitar a concessão criaria a promessa de um acesso que o guard de papel
    nega em seguida — o contador veria o módulo na tela e continuaria tomando
    403.
    """
    csrf = await _login(client, tenant, usuario)

    resposta = await client.post(
        _url(empresa.id),
        json={"usuario_id": str(contador.id), "modulos": "auditoria"},
        headers={"X-CSRF-Token": csrf},
    )

    assert resposta.status_code == 422

