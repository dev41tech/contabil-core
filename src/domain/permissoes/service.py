"""Serviço de Permissões por empresa.

Regras de negócio:
- Apenas admins podem gerenciar permissões.
- Não é possível conceder/revogar permissão ao próprio admin logado (sem-efeito
  prático, pois admins têm acesso irrestrito, mas evitamos confusão).
- O usuário deve pertencer ao mesmo tenant que a empresa.
- Permissão duplicada (mesmo usuario_id + empresa_id) retorna ConflictError.
- Ao revogar, simplesmente deleta a linha (não há soft-delete em Permissao).
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError, ValidationError
from src.db.models import Empresa, Permissao, Usuario
from src.schemas.permissoes import (
    PermissaoCreate,
    PermissaoListResponse,
    PermissaoResponse,
    PermissaoUpdate,
)

logger = structlog.get_logger(__name__)


class PermissaoService:
    def __init__(
        self, db: AsyncSession, empresa_id: UUID, tenant_id: UUID
    ) -> None:
        self._db = db
        self._empresa_id = empresa_id
        self._tenant_id = tenant_id

    # ── consultas ────────────────────────────────────────────────────────────

    async def listar(self) -> PermissaoListResponse:
        rows = (
            await self._db.execute(
                select(Permissao, Usuario)
                .join(Usuario, Usuario.id == Permissao.usuario_id)
                .where(Permissao.empresa_id == self._empresa_id)
                .order_by(Usuario.nome)
            )
        ).all()

        items = [
            PermissaoResponse(
                usuario_id=p.usuario_id,
                empresa_id=p.empresa_id,
                modulos=p.modulos,
                usuario_nome=u.nome,
                usuario_email=u.email,
                usuario_role=u.role,
                usuario_ativo=u.ativo,
            )
            for p, u in rows
        ]
        return PermissaoListResponse(items=items, total=len(items))

    # ── mutações ─────────────────────────────────────────────────────────────

    async def conceder(self, data: PermissaoCreate) -> PermissaoResponse:
        usuario = await self._get_usuario_or_404(data.usuario_id)

        # Verifica se já existe
        existente = await self._db.execute(
            select(Permissao).where(
                Permissao.usuario_id == data.usuario_id,
                Permissao.empresa_id == self._empresa_id,
            )
        )
        if existente.scalar_one_or_none():
            raise ConflictError(
                message=f"Usuário '{usuario.email}' já possui acesso a esta empresa."
            )

        p = Permissao(
            usuario_id=data.usuario_id,
            empresa_id=self._empresa_id,
            modulos=data.modulos,
        )
        self._db.add(p)
        await self._db.flush()

        logger.info(
            "permissao.concedida",
            usuario_id=str(data.usuario_id),
            empresa_id=str(self._empresa_id),
            modulos=data.modulos,
        )
        return PermissaoResponse(
            usuario_id=p.usuario_id,
            empresa_id=p.empresa_id,
            modulos=p.modulos,
            usuario_nome=usuario.nome,
            usuario_email=usuario.email,
            usuario_role=usuario.role,
            usuario_ativo=usuario.ativo,
        )

    async def atualizar(
        self, usuario_id: UUID, data: PermissaoUpdate
    ) -> PermissaoResponse:
        p, usuario = await self._get_or_404(usuario_id)
        p.modulos = data.modulos
        await self._db.flush()

        logger.info(
            "permissao.atualizada",
            usuario_id=str(usuario_id),
            empresa_id=str(self._empresa_id),
            modulos=data.modulos,
        )
        return PermissaoResponse(
            usuario_id=p.usuario_id,
            empresa_id=p.empresa_id,
            modulos=p.modulos,
            usuario_nome=usuario.nome,
            usuario_email=usuario.email,
            usuario_role=usuario.role,
            usuario_ativo=usuario.ativo,
        )

    async def revogar(self, usuario_id: UUID) -> None:
        p, usuario = await self._get_or_404(usuario_id)
        await self._db.delete(p)
        await self._db.flush()
        logger.info(
            "permissao.revogada",
            usuario_id=str(usuario_id),
            empresa_id=str(self._empresa_id),
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _get_or_404(self, usuario_id: UUID) -> tuple[Permissao, Usuario]:
        row = (
            await self._db.execute(
                select(Permissao, Usuario)
                .join(Usuario, Usuario.id == Permissao.usuario_id)
                .where(
                    Permissao.usuario_id == usuario_id,
                    Permissao.empresa_id == self._empresa_id,
                )
            )
        ).one_or_none()
        if not row:
            raise NotFoundError(
                message="Permissão não encontrada para este usuário/empresa."
            )
        return row

    async def _get_usuario_or_404(self, usuario_id: UUID) -> Usuario:
        usuario = (
            await self._db.execute(
                select(Usuario).where(
                    Usuario.id == usuario_id,
                    Usuario.tenant_id == self._tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not usuario:
            raise NotFoundError(
                message="Usuário não encontrado neste escritório."
            )
        return usuario
