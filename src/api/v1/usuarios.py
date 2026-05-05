"""Router de usuários — /api/v1/usuarios

Apenas administradores do escritório podem listar, criar e desativar usuários.
Um usuário não pode desativar a si mesmo.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_admin, require_csrf
from src.core.security import hash_password
from src.db.models import Usuario
from src.db.session import get_db
from src.schemas.usuarios import UsuarioCreate, UsuarioListResponse, UsuarioResponse

router = APIRouter(prefix="/usuarios", tags=["usuarios"])


@router.get("", response_model=UsuarioListResponse)
async def listar_usuarios(
    page: int = 1,
    page_size: int = 50,
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UsuarioListResponse:
    """Lista todos os usuários do escritório (apenas admins)."""
    offset = (page - 1) * page_size

    count_result = await db.execute(
        select(func.count()).select_from(Usuario).where(
            Usuario.tenant_id == ctx.tenant_id,
            Usuario.deleted_at.is_(None),
        )
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Usuario)
        .where(
            Usuario.tenant_id == ctx.tenant_id,
            Usuario.deleted_at.is_(None),
        )
        .order_by(Usuario.nome)
        .offset(offset)
        .limit(page_size)
    )
    usuarios = result.scalars().all()

    return UsuarioListResponse(
        items=[UsuarioResponse.model_validate(u) for u in usuarios],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=UsuarioResponse, status_code=201, dependencies=[Depends(require_csrf)])
async def criar_usuario(
    body: UsuarioCreate,
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UsuarioResponse:
    """Cria um novo usuário no escritório (apenas admins)."""
    # Verificar duplicata
    existing = await db.execute(
        select(Usuario).where(
            Usuario.tenant_id == ctx.tenant_id,
            Usuario.email == body.email.lower().strip(),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Já existe um usuário com este e-mail neste escritório.")

    if body.role not in ("admin", "contador"):
        raise HTTPException(status_code=422, detail="Role inválida. Use 'admin' ou 'contador'.")

    usuario = Usuario(
        tenant_id=ctx.tenant_id,
        email=body.email.lower().strip(),
        nome=body.nome.strip(),
        senha_hash=hash_password(body.senha),
        role=body.role,
        ativo=True,
    )
    db.add(usuario)
    await db.commit()
    await db.refresh(usuario)
    return UsuarioResponse.model_validate(usuario)


@router.patch("/{usuario_id}/desativar", response_model=UsuarioResponse, dependencies=[Depends(require_csrf)])
async def desativar_usuario(
    usuario_id: UUID,
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UsuarioResponse:
    """Desativa um usuário do escritório (apenas admins). Não é possível desativar a si mesmo."""
    if usuario_id == ctx.user_id:
        raise HTTPException(status_code=400, detail="Você não pode desativar sua própria conta.")

    result = await db.execute(
        select(Usuario).where(
            Usuario.id == usuario_id,
            Usuario.tenant_id == ctx.tenant_id,
            Usuario.deleted_at.is_(None),
        )
    )
    usuario = result.scalar_one_or_none()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    usuario.ativo = False
    await db.commit()
    await db.refresh(usuario)
    return UsuarioResponse.model_validate(usuario)


@router.patch("/{usuario_id}/reativar", response_model=UsuarioResponse, dependencies=[Depends(require_csrf)])
async def reativar_usuario(
    usuario_id: UUID,
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UsuarioResponse:
    """Reativa um usuário desativado (apenas admins)."""
    result = await db.execute(
        select(Usuario).where(
            Usuario.id == usuario_id,
            Usuario.tenant_id == ctx.tenant_id,
            Usuario.deleted_at.is_(None),
        )
    )
    usuario = result.scalar_one_or_none()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    usuario.ativo = True
    await db.commit()
    await db.refresh(usuario)
    return UsuarioResponse.model_validate(usuario)
