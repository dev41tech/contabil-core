"""Catálogo de permissões — `recurso.ação`.

POR QUE ISTO EXISTE

A autorização hoje adivinha o módulo pela URL: `get_company_context` pega o
primeiro segmento depois de `/empresas/{id}/` e compara com a lista CSV do
usuário. Duas consequências, as duas já observadas neste repositório:

1. rota nova nasce com a política que o caminho dela sugerir, sem ninguém
   decidir — e se o nome não estiver na whitelist, ela nasce inalcançável.
   Foi o que aconteceu com `concilpro` e `aplicacoes_financeiras`;
2. renomear um prefixo muda silenciosamente quem pode chamar o quê.

Com o catálogo, a política vira declaração explícita no ponto onde o risco é
criado: a própria rota. Ver `src/api/autorizacao.py`.

AS QUATRO AÇÕES

`read`     consultar, listar, exportar uma representação que já existe
`write`    criar ou alterar cadastro reversível
`execute`  iniciar operação com efeito financeiro/contábil, ou desfazê-la
`manage`   administrar acesso, configuração estrutural ou identidade

Não existe `approve` de propósito. Aprovar exige objeto pendente, autor
diferente do aprovador, estados e rejeição; hoje o backend executa a operação
na própria requisição. Chamar `execute` de `approve` daria aparência de dupla
conferência sem entregá-la.

O QUE ESTE MÓDULO **NÃO** FAZ

Não decide quem tem a permissão. No modelo atual, ter o módulo do recurso
equivale a ter todas as ações dele — a separação por ação só passa a valer
quando a tabela de papéis existir. O catálogo é a declaração; a avaliação mora
em `src/api/autorizacao.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

ACOES = ("read", "write", "execute", "manage")


@dataclass(frozen=True)
class Permissao:
    recurso: str
    acao: str

    @property
    def codigo(self) -> str:
        return f"{self.recurso}.{self.acao}"

    def __str__(self) -> str:  # pragma: no cover - conveniência de log
        return self.codigo


# Ações que fazem sentido em cada recurso. Declarar recurso × ação aqui é o que
# impede uma rota de pedir `neo.approve` ou `stats.execute` e o erro só
# aparecer em produção: `requer()` levanta na importação do módulo da rota, ou
# seja, no start da aplicação e na coleta dos testes.
#
# O nome do recurso é o mesmo do módulo em `MODULOS_VALIDOS` — é o que permite
# o adaptador traduzir para o CSV atual sem tabela de-para.
_CATALOGO: dict[str, tuple[str, ...]] = {
    "agencias": ("read", "write"),
    "aplicacoes_financeiras": ("read", "write"),
    "auditoria": ("read",),
    "cartoes": ("read", "write", "execute"),
    "comprovantes": ("read", "write"),
    "concilpro": ("read", "execute"),
    # Lançamento manual e, quando existir, estorno.
    "contabil": ("read", "write", "execute"),
    "contrapartes": ("read", "write"),
    # `execute`: gerar exportação persiste um ExportJob — é mutação, não leitura.
    "exportacao": ("read", "execute"),
    # `execute`: importar extrato e cancelar lote — cria e apaga movimento.
    "extrato": ("read", "execute"),
    "jobs": ("read",),
    # `execute`: processar, classificar, reclassificar, cancelar, liberar.
    "neo": ("read", "execute"),
    "notas": ("read", "write"),
    "openbanking": ("read", "write", "execute"),
    # `manage`: importação em massa e exclusão estrutural de conta.
    "plano_contas": ("read", "write", "manage"),
    "permissoes": ("read", "manage"),
    "regras": ("read", "write"),
    "relatorios": ("read",),
    "stats": ("read",),
}

PERMISSOES: dict[str, Permissao] = {
    f"{recurso}.{acao}": Permissao(recurso=recurso, acao=acao)
    for recurso, acoes in _CATALOGO.items()
    for acao in acoes
}


class PermissaoDesconhecida(LookupError):
    """Código fora do catálogo. É erro de programação, não de runtime."""


def permissao(codigo: str) -> Permissao:
    """Resolve o código, ou explode dizendo o que existe para aquele recurso."""
    achada = PERMISSOES.get(codigo)
    if achada is not None:
        return achada

    recurso = codigo.split(".", 1)[0]
    disponiveis = _CATALOGO.get(recurso)
    if disponiveis is None:
        raise PermissaoDesconhecida(
            f"Recurso '{recurso}' não está no catálogo ({codigo!r}). "
            f"Recursos: {sorted(_CATALOGO)}"
        )
    raise PermissaoDesconhecida(
        f"Ação inválida para '{recurso}': {codigo!r}. "
        f"Disponíveis: {[f'{recurso}.{a}' for a in disponiveis]}"
    )
