"""A rota declara o que exige — e a declaração é o que autoriza.

    @router.post(
        "/pendencias/classificar-lote",
        dependencies=[Depends(require_csrf), requer("neo.execute")],
    )

POR QUE NÃO CONTINUAR DERIVANDO DO CAMINHO

`get_company_context` resolve o módulo pelo primeiro segmento depois de
`/empresas/{id}/`. Funciona até alguém criar uma rota: ela nasce com a política
que o caminho sugerir, sem ninguém ter decidido — e se o nome não estiver na
whitelist de módulos, nasce inalcançável para qualquer contador. Foi assim que
`concilpro` e `aplicacoes_financeiras` passaram meses só acessíveis via `"*"`.

Declarar move a decisão para onde o risco é criado: a linha que expõe o
endpoint. E torna a política inspecionável — `test_autorizacao.py` percorre
`app.routes` e cobra declaração de toda rota de empresa.

O QUE MUDA DE COMPORTAMENTO AGORA: NADA

Esta é a etapa de adaptação. No modelo atual, ter o módulo do recurso equivale
a ter todas as ações dele, então `neo.read` e `neo.execute` autorizam o mesmo
conjunto de pessoas — exatamente como hoje. A separação por ação só passa a
valer quando existir a tabela de papéis; até lá o que se ganha é a declaração
registrada, verificável e impossível de esquecer numa rota nova.

Trocar a ordem — quebrar as ações antes de declarar as rotas — deixaria a
aplicação sem saber qual ação cada endpoint exige, e a resposta seria adivinhar
pelo método HTTP.
"""

from __future__ import annotations

import structlog
from fastapi import Depends, params

from src.api.deps import AuthContext, get_company_context
from src.core.errors import ForbiddenError
from src.core.permissoes import Permissao, permissao

logger = structlog.get_logger(__name__)

# Atributo que marca a dependência como declaração de permissão. O teste de
# introspecção procura por ele; sem marcador, a única forma de saber o que uma
# rota exige seria ler o corpo dela.
ATRIBUTO_PERMISSAO = "_permissao_exigida"


def requer(codigo: str) -> params.Depends:
    """Declara a permissão exigida pela rota.

    Levanta na importação se o código não existir no catálogo — ou seja, no
    start da aplicação e na coleta dos testes, nunca no meio de uma requisição
    de um contador.
    """
    exigida: Permissao = permissao(codigo)

    async def verificar_permissao(
        ctx: AuthContext = Depends(get_company_context),
    ) -> AuthContext:
        # `get_company_context` já garantiu tenant, empresa ativa e o módulo do
        # CAMINHO. Aqui a checagem é sobre o recurso DECLARADO, que é o que
        # passa a valer: quando os dois divergem, quem manda é a declaração.
        modulos = getattr(ctx, "modulos", None)
        if modulos is None:
            # Contexto sem módulos resolvidos é admin ou rota fora do padrão de
            # empresa. Negar aqui esconderia o caso legítimo; o guard do
            # caminho já rodou.
            return ctx

        if "*" in modulos or exigida.recurso in modulos:
            return ctx

        logger.info(
            "autorizacao.negada",
            permissao=exigida.codigo,
            recurso=exigida.recurso,
            user_id=str(ctx.user_id),
        )
        raise ForbiddenError(
            message=f"Sem permissão para {exigida.recurso} nesta empresa."
        )

    setattr(verificar_permissao, ATRIBUTO_PERMISSAO, exigida)
    return Depends(verificar_permissao)


def permissao_declarada(rota) -> Permissao | None:
    """A permissão que a rota declara, se declara. Usado pelo teste-catraca."""
    dependant = getattr(rota, "dependant", None)
    if dependant is None:
        return None

    def procurar(dep) -> Permissao | None:
        achada = getattr(dep.call, ATRIBUTO_PERMISSAO, None)
        if achada is not None:
            return achada
        for sub in dep.dependencies:
            if (encontrada := procurar(sub)) is not None:
                return encontrada
        return None

    return procurar(dependant)
