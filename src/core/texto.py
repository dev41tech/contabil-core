"""Normalização de texto contábil exibido.

Existe porque contadores relataram históricos com espaçamento e capitalização
inconsistentes — dependem de como o banco formatou o extrato ou de como a
regra foi digitada. Aplica-se apenas ao texto contábil *gerado*
(`RegistroContabil.historico`/`descricao`) — nunca ao texto bruto do extrato
(`Transacao.historico`, `RegistroContabil.historico_extrato`), que precisa
permanecer intacto para auditoria e conferência com o documento original.
"""

from __future__ import annotations


def normalizar_historico_contabil(texto: str) -> str:
    """Colapsa espaços (incluindo tabs/quebras de linha) e converte para maiúsculas."""
    return " ".join(texto.split()).upper()
