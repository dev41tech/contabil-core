"""A política de acesso de cada rota é declarada, e a declaração é cobrada.

Antes disto, a autorização era adivinhada pelo caminho da URL: rota nova nascia
com a política que o prefixo dela sugerisse, sem ninguém decidir. `concilpro` e
`aplicacoes_financeiras` ficaram meses acessíveis só via `"*"` por causa disso,
e ninguém tinha como perceber.

O teste central aqui é uma CATRACA: as rotas que ainda não declaram estão
listadas uma a uma. A lista só encolhe. Rota nova que não declare permissão
falha o teste — e a única forma de "resolver" é declarar ou acrescentar à
lista, o que aparece na revisão do PR.
"""

from __future__ import annotations

import pytest

from src.api.app import create_app
from src.api.autorizacao import permissao_declarada
from src.core.permissoes import PERMISSOES, PermissaoDesconhecida, permissao

PREFIXO_EMPRESA = "/empresas/{empresa_id}/"

# Rotas de empresa que ainda não declaram permissão. Herança da autorização por
# caminho — continuam protegidas por `get_company_context`, que resolve o módulo
# pela URL, mas ninguém escolheu a política delas.
#
# ESTA LISTA SÓ ENCOLHE. Não acrescente nada aqui sem dizer no PR por quê.
_AINDA_NAO_DECLARAM = {
    "agencias",
    "aplicacoes-financeiras",
    "auditoria",
    "cartoes",
    "comprovantes",
    "concilpro",
    "contabil",
    "contrapartes",
    "exportacao",
    "jobs",
    "notas",
    "openbanking",
    "permissoes",
    "plano-contas",
    "regras",
    "relatorios",
    "stats",
}


def _rotas_de_empresa():
    return [
        rota
        for rota in create_app().routes
        if PREFIXO_EMPRESA in getattr(rota, "path", "") and hasattr(rota, "methods")
    ]


def _modulo_do_caminho(caminho: str) -> str:
    return caminho.split(PREFIXO_EMPRESA, 1)[1].split("/", 1)[0]


def test_rota_de_empresa_declara_a_permissao_que_exige():
    """A catraca. Rota nova nasce declarando, ou o teste barra."""
    sem_declaracao = {
        _modulo_do_caminho(rota.path)
        for rota in _rotas_de_empresa()
        if permissao_declarada(rota) is None
    }

    novas = sem_declaracao - _AINDA_NAO_DECLARAM
    assert not novas, (
        f"Rotas sem permissão declarada em módulos que já deviam declarar: "
        f"{sorted(novas)}. Use `dependencies=[requer('recurso.acao')]`."
    )


def test_a_catraca_nao_afrouxou():
    """Módulo que já declarou não pode voltar para a lista de exceção.

    Sem isto, a lista poderia crescer de volta silenciosamente — e uma catraca
    que anda para trás não é catraca.
    """
    declaram = {
        _modulo_do_caminho(rota.path)
        for rota in _rotas_de_empresa()
        if permissao_declarada(rota) is not None
    }

    regressao = declaram & _AINDA_NAO_DECLARAM
    assert not regressao, (
        f"Estes módulos já declaram permissão e continuam na lista de exceção: "
        f"{sorted(regressao)}. Remova-os de _AINDA_NAO_DECLARAM."
    )


def test_recurso_declarado_bate_com_o_modulo_do_caminho():
    """Enquanto `get_company_context` ainda resolve pelo caminho, os dois têm de
    concordar — senão uma rota exigiria `neo` e o guard do caminho cobraria
    `extrato`, e o resultado seria a interseção, não a declaração.

    Quando o guard por caminho sair, este teste sai com ele.
    """
    for rota in _rotas_de_empresa():
        declarada = permissao_declarada(rota)
        if declarada is None:
            continue
        modulo = _modulo_do_caminho(rota.path).replace("-", "_")
        assert declarada.recurso == modulo, (
            f"{rota.path} declara {declarada.codigo} mas o caminho resolve "
            f"para o módulo '{modulo}'"
        )


def test_metodo_que_muda_estado_nao_declara_apenas_leitura():
    """POST/PUT/PATCH/DELETE pedindo `.read` é engano de declaração.

    A exceção é o POST que só consulta — `simular-regra` responde o que uma
    regra faria, sem gravar nada. Está aqui nomeado, e não por regra geral,
    justamente para o próximo caso do tipo ser uma decisão consciente.
    """
    consultas_por_post = {"/neo/pendencias/simular-regra"}

    for rota in _rotas_de_empresa():
        declarada = permissao_declarada(rota)
        if declarada is None or declarada.acao != "read":
            continue
        if not (rota.methods - {"GET", "HEAD", "OPTIONS"}):
            continue
        assert any(rota.path.endswith(p) for p in consultas_por_post), (
            f"{sorted(rota.methods)} {rota.path} muda estado e declara "
            f"{declarada.codigo}"
        )


def test_catalogo_recusa_codigo_inexistente():
    """O erro aparece na importação da rota, não na requisição do contador."""
    with pytest.raises(PermissaoDesconhecida) as erro:
        permissao("neo.approve")
    assert "neo.read" in str(erro.value), "a mensagem diz o que existe"

    with pytest.raises(PermissaoDesconhecida):
        permissao("inexistente.read")


def test_todo_recurso_do_catalogo_permite_leitura():
    """Recurso sem `read` seria recurso que ninguém consegue consultar."""
    recursos = {p.recurso for p in PERMISSOES.values()}
    sem_leitura = {r for r in recursos if f"{r}.read" not in PERMISSOES}
    assert not sem_leitura


@pytest.mark.asyncio
async def test_contador_sem_o_modulo_declarado_recebe_403(
    client, db, tenant, usuario, empresa
):
    """A negação continua valendo com a declaração no caminho.

    Este é o teste que garante que declarar não afrouxou nada: um contador sem
    o módulo `neo` continua barrado na fila de classificação.
    """
    from src.core.security import hash_password
    from src.db.models import Permissao as PermissaoModel
    from src.db.models import Usuario

    contador = Usuario(
        tenant_id=tenant.id,
        email="sem.neo@41contabil.com.br",
        nome="Contador Sem NEO",
        senha_hash=hash_password("senha_segura_123"),
        role="contador",
    )
    db.add(contador)
    await db.flush()
    db.add(
        PermissaoModel(
            usuario_id=contador.id, empresa_id=empresa.id, modulos="extrato"
        )
    )
    await db.flush()

    entrada = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_id": str(tenant.id),
            "email": contador.email,
            "senha": "senha_segura_123",
        },
    )
    assert entrada.status_code == 200

    negado = await client.get(f"/api/v1/empresas/{empresa.id}/neo/pendencias")
    assert negado.status_code == 403

    permitido = await client.get(f"/api/v1/empresas/{empresa.id}/extrato")
    assert permitido.status_code == 200
