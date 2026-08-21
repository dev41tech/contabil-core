"""Escrita centralizada do trilho de auditoria."""

from src.domain.auditoria.service import listar_auditoria, registrar_auditoria

__all__ = ["listar_auditoria", "registrar_auditoria"]
