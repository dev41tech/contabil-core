"""Schemas Pydantic para autenticação."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    tenant_id: UUID = Field(..., description="ID do escritório contábil")
    email: EmailStr
    senha: str = Field(..., min_length=8)


class TrocarSenhaRequest(BaseModel):
    """Troca da própria senha.

    A senha atual é exigida mesmo com a sessão já autenticada: sessão roubada
    não pode virar conta roubada. Sem isso, quem pegasse o cookie trocaria a
    senha e expulsaria o dono.
    """

    senha_atual: str = Field(..., min_length=8)
    nova_senha: str = Field(..., min_length=8, max_length=200)


class ResetarSenhaRequest(BaseModel):
    """Reset feito por administrador, para quem perdeu o acesso.

    Não pede a senha atual — é justamente o caso de quem não a tem. Por isso a
    operação é registrada na auditoria com quem resetou e para quem: é a única
    trilha de que a senha de outra pessoa foi trocada por um terceiro.
    """

    nova_senha: str = Field(..., min_length=8, max_length=200)


class LoginResponse(BaseModel):
    message: str = "Login realizado com sucesso."
    # Tokens trafegam em cookies HttpOnly — não no body.
    # O body retorna apenas o CSRF token para uso em mutações.
    csrf_token: str


class RefreshResponse(BaseModel):
    message: str = "Tokens renovados com sucesso."
    csrf_token: str


class MeResponse(BaseModel):
    user_id: UUID
    email: str
    nome: str
    role: str
    tenant_id: UUID
    # O token de CSRF só era entregue no corpo do login e do refresh. Uma aba
    # aberta com a sessão já válida não faz nenhum dos dois — e, com o frontend
    # em outra origem, ela não consegue ler o cookie. Subia sem token e falhava
    # na primeira gravação. `/auth/me` é o que toda aba chama no boot, então é
    # daqui que ela passa a se abastecer.
    #
    # `None` quando o cookie não veio: aí a aba precisa renovar a sessão, e a
    # distinção importa para ela saber qual dos dois caminhos seguir.
    csrf_token: str | None = None
