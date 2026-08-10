"""Agrega todos os routers da versão 1."""

from fastapi import APIRouter

from src.api.v1.agencias import router as agencias_router
from src.api.v1.aplicacoes import router as aplicacoes_router
from src.api.v1.auth import router as auth_router
from src.api.v1.comprovantes import router as comprovantes_router
from src.api.v1.contabil import router as contabil_router
from src.api.v1.empresas import router as empresas_router
from src.api.v1.exportacao import router as exportacao_router
from src.api.v1.extrato import router as extrato_router
from src.api.v1.neo import router as neo_router
from src.api.v1.notas import router as notas_router
from src.api.v1.plano_contas import router as plano_contas_router
from src.api.v1.regras import router as regras_router
from src.api.v1.usuarios import router as usuarios_router
from src.api.v1.stats import router as stats_router
from src.api.v1.permissoes import router as permissoes_router
from src.api.v1.cartoes import router as cartoes_router
from src.api.v1.openbanking import router as openbanking_router
from src.api.v1.relatorios import router as relatorios_router
from src.api.v1.concilpro import router as concilpro_router

router = APIRouter(prefix="/api/v1")

router.include_router(auth_router)
router.include_router(usuarios_router)
router.include_router(empresas_router)
router.include_router(agencias_router)
router.include_router(aplicacoes_router)
router.include_router(plano_contas_router)
router.include_router(regras_router)
router.include_router(extrato_router)
router.include_router(notas_router)
router.include_router(comprovantes_router)
router.include_router(neo_router)
router.include_router(contabil_router)
router.include_router(exportacao_router)
router.include_router(stats_router)
router.include_router(permissoes_router)
router.include_router(cartoes_router)
router.include_router(openbanking_router)
router.include_router(relatorios_router)
router.include_router(concilpro_router)
