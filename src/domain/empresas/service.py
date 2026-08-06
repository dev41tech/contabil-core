"""Serviço de gerenciamento de empresas."""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.cnpj import valido as cnpj_valido
from src.core.errors import ConflictError, EmpresaNaoEncontradaError
from src.db.models import Empresa
from src.schemas.empresas import (
    CnpjInvalidoListResponse,
    EmpresaCnpjInvalidoResponse,
    EmpresaCreate,
    EmpresaListResponse,
    EmpresaResponse,
    EmpresaUpdate,
)

logger = structlog.get_logger(__name__)


class EmpresaService:
    def __init__(self, db: AsyncSession, tenant_id: UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def listar(self, page: int = 1, page_size: int = 50) -> EmpresaListResponse:
        offset = (page - 1) * page_size

        count_q = select(func.count()).where(
            Empresa.tenant_id == self._tenant_id,
            Empresa.deleted_at == None,
        )
        total = (await self._db.execute(count_q)).scalar_one()

        rows_q = (
            select(Empresa)
            .where(Empresa.tenant_id == self._tenant_id, Empresa.deleted_at == None)
            .order_by(Empresa.razao_social)
            .offset(offset)
            .limit(page_size)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()

        return EmpresaListResponse(
            items=[EmpresaResponse.model_validate(e) for e in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def listar_cnpj_invalidos(self) -> CnpjInvalidoListResponse:
        """Empresas cujo CNPJ cadastrado não passa na validação de dígito verificador.

        O cadastro só passou a validar DV depois da migração, então as empresas
        importadas pelo `scripts/import_mrcont.py` carregam CNPJ sintético gerado a
        partir do índice da pasta. Para elas a exportação de notas volta vazia e
        silenciosa — esta listagem é o que torna o problema visível.

        Filtra em Python porque o dígito verificador não é expressável em SQL; o
        cadastro tem dezenas de empresas por escritório, não milhões.
        """
        rows = (
            await self._db.execute(
                select(Empresa)
                .where(Empresa.tenant_id == self._tenant_id, Empresa.deleted_at == None)
                .order_by(Empresa.razao_social)
            )
        ).scalars().all()

        invalidas = [e for e in rows if not cnpj_valido(e.cnpj)]

        return CnpjInvalidoListResponse(
            items=[EmpresaCnpjInvalidoResponse.model_validate(e) for e in invalidas],
            total=len(invalidas),
            total_empresas=len(rows),
        )

    async def obter(self, empresa_id: UUID) -> EmpresaResponse:
        empresa = await self._get_or_404(empresa_id)
        return EmpresaResponse.model_validate(empresa)

    async def criar(self, data: EmpresaCreate) -> EmpresaResponse:
        # Verifica CNPJ duplicado no tenant
        existing = await self._db.execute(
            select(Empresa).where(
                Empresa.tenant_id == self._tenant_id,
                Empresa.cnpj == data.cnpj,
                Empresa.deleted_at == None,
            )
        )
        duplicada = existing.scalar_one_or_none()
        if duplicada:
            # A mensagem nomeia a empresa existente de propósito. Repetir só o
            # CNPJ que o usuário acabou de digitar não informa nada: o caso comum
            # é a mesma empresa já cadastrada com outra grafia da razão social, e
            # sem o nome não dá para perceber isso na tela.
            raise ConflictError(
                message=(
                    f"O CNPJ {data.cnpj} já está cadastrado para "
                    f"\"{duplicada.razao_social}\". Se for a mesma empresa, edite "
                    f"o cadastro dela em vez de criar uma nova."
                ),
                details={
                    "empresa_id": str(duplicada.id),
                    "razao_social": duplicada.razao_social,
                    "cnpj": duplicada.cnpj,
                },
            )

        empresa = Empresa(
            tenant_id=self._tenant_id,
            razao_social=data.razao_social.strip(),
            cnpj=data.cnpj,
            regime_tributario=data.regime_tributario,
        )
        self._db.add(empresa)
        await self._db.flush()

        logger.info("empresa.criada", empresa_id=str(empresa.id), cnpj=empresa.cnpj)
        return EmpresaResponse.model_validate(empresa)

    async def atualizar(self, empresa_id: UUID, data: EmpresaUpdate) -> EmpresaResponse:
        empresa = await self._get_or_404(empresa_id)

        if data.razao_social is not None:
            empresa.razao_social = data.razao_social.strip()
        if data.regime_tributario is not None:
            empresa.regime_tributario = data.regime_tributario

        await self._db.flush()
        logger.info("empresa.atualizada", empresa_id=str(empresa_id))
        return EmpresaResponse.model_validate(empresa)

    async def desativar(self, empresa_id: UUID) -> None:
        empresa = await self._get_or_404(empresa_id)
        empresa.ativa = False
        await self._db.flush()
        logger.info("empresa.desativada", empresa_id=str(empresa_id))

    async def _get_or_404(self, empresa_id: UUID) -> Empresa:
        result = await self._db.execute(
            select(Empresa).where(
                Empresa.id == empresa_id,
                Empresa.tenant_id == self._tenant_id,
                Empresa.deleted_at == None,
            )
        )
        empresa = result.scalar_one_or_none()
        if not empresa:
            raise EmpresaNaoEncontradaError()
        return empresa
