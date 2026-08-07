"""Schemas Pydantic para Permissões por empresa."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

MODULOS_VALIDOS = frozenset(
    [
        "agencias",
        "cartoes",
        "comprovantes",
        "contabil",
        "exportacao",
        "extrato",
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


class PermissaoCreate(BaseModel):
    usuario_id: UUID
    modulos: str = Field(
        default="*",
        description=(
            "Módulos separados por vírgula. '*' = acesso total. "
            "Opções: agencias, cartoes, comprovantes, contabil, exportacao, "
            "extrato, neo, notas, openbanking, plano_contas, regras, relatorios, stats"
        ),
    )

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

    @field_validator("modulos")
    @classmethod
    def valida_modulos(cls, v: str) -> str:
        return PermissaoCreate.valida_modulos(v)


class PermissaoResponse(BaseModel):
    usuario_id: UUID
    empresa_id: UUID
    modulos: str
    # Dados expandidos do usuário
    usuario_nome: str | None = None
    usuario_email: str | None = None
    usuario_role: str | None = None
    usuario_ativo: bool | None = None

    model_config = {"from_attributes": True}


class PermissaoListResponse(BaseModel):
    items: list[PermissaoResponse]
    total: int
