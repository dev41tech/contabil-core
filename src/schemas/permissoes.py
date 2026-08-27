"""Schemas Pydantic para Permissões por empresa."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.core.permissoes import PAPEIS, PAPEL_PADRAO

# Módulos que um contador pode receber. O nome é o primeiro segmento da URL
# depois de `/empresas/{id}/`, com hífen virando underscore — é assim que
# `get_company_context` resolve o módulo da requisição.
#
# Faltar aqui não é detalhe: a validação recusa conceder o que não está na
# lista, então o administrador é EMPURRADO para o `"*"` — dar tudo — quando
# queria dar um módulo só. Foi o que aconteceu com `concilpro` e
# `aplicacoes_financeiras`, que existem como rota desde sempre e nunca puderam
# ser concedidos isoladamente.
MODULOS_VALIDOS = frozenset(
    [
        "agencias",
        "aplicacoes_financeiras",
        "cartoes",
        "comprovantes",
        "concilpro",
        "contabil",
        "contrapartes",
        "exportacao",
        "extrato",
        "jobs",
        "neo",
        "notas",
        "openbanking",
        "plano_contas",
        "regras",
        "relatorios",
        "stats",
        "*",
    ]
)

# Módulos que existem como rota e NÃO são concedíveis, porque a própria rota
# exige papel de administrador (`get_admin_company_context` / `require_admin`).
#
# Ficam fora de `MODULOS_VALIDOS` de propósito: aceitar a concessão criaria a
# promessa de um acesso que o guard de papel nega em seguida — o contador
# receberia o módulo na tela de permissões e continuaria tomando 403.
#
# O teste `test_permissoes.py::test_todo_modulo_de_rota_e_concedivel_ou_admin`
# confronta esta lista com as rotas reais: módulo novo precisa cair num dos
# dois lados, nunca em nenhum.
MODULOS_SOMENTE_ADMIN = frozenset(["auditoria", "permissoes"])


class PermissaoCreate(BaseModel):
    usuario_id: UUID
    papel: str = Field(
        default=PAPEL_PADRAO,
        description=(
            f"Papel na empresa: {', '.join(sorted(PAPEIS))}. "
            "Módulo diz ONDE a pessoa entra; papel diz O QUANTO ela faz lá "
            "dentro. O padrão é 'contador' — quem precisa reestruturar plano "
            "de contas ou importar em massa recebe 'dono' explicitamente."
        ),
    )
    modulos: str = Field(
        default="*",
        description=(
            "Módulos separados por vírgula. '*' = acesso total. "
            "Opções: agencias, cartoes, comprovantes, contabil, contrapartes, exportacao, "
            "extrato, jobs, neo, notas, openbanking, plano_contas, regras, relatorios, stats"
        ),
    )

    @field_validator("papel")
    @classmethod
    def valida_papel(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in PAPEIS:
            raise ValueError(f"Papel inválido: {v!r}. Opções: {sorted(PAPEIS)}")
        return v

    @field_validator("modulos")
    @classmethod
    def valida_modulos(cls, v: str) -> str:
        v = v.strip().lower()
        if v == "*":
            return v
        partes = {m.strip() for m in v.split(",") if m.strip()}
        invalidos = partes - MODULOS_VALIDOS
        if invalidos:
            raise ValueError(
                f"Módulos inválidos: {invalidos}. "
                f"Opções: {sorted(MODULOS_VALIDOS - {'*'})}"
            )
        return ",".join(sorted(partes))


class PermissaoUpdate(BaseModel):
    modulos: str = Field(default="*")
    papel: str = Field(default=PAPEL_PADRAO)

    @field_validator("papel")
    @classmethod
    def valida_papel(cls, v: str) -> str:
        return PermissaoCreate.valida_papel(v)

    @field_validator("modulos")
    @classmethod
    def valida_modulos(cls, v: str) -> str:
        return PermissaoCreate.valida_modulos(v)


class PermissaoResponse(BaseModel):
    usuario_id: UUID
    empresa_id: UUID
    modulos: str
    papel: str
    # Dados expandidos do usuário
    usuario_nome: str | None = None
    usuario_email: str | None = None
    usuario_role: str | None = None
    usuario_ativo: bool | None = None

    model_config = {"from_attributes": True}


class PermissaoListResponse(BaseModel):
    items: list[PermissaoResponse]
    total: int
