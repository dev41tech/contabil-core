"""Normalização de texto contábil exibido.

Existe porque contadores relataram históricos com espaçamento e capitalização
inconsistentes — dependem de como o banco formatou o extrato ou de como a
regra foi digitada. Aplica-se apenas ao texto contábil *gerado*
(`RegistroContabil.historico`/`descricao`) — nunca ao texto bruto do extrato
(`Transacao.historico`, `RegistroContabil.historico_extrato`), que precisa
permanecer intacto para auditoria e conferência com o documento original.
"""

from __future__ import annotations

import re
import unicodedata

_NAO_ALFANUMERICO = re.compile(r"[^a-z0-9]+")


def normalizar_historico_contabil(texto: str) -> str:
    """Colapsa espaços (incluindo tabs/quebras de linha) e converte para maiúsculas."""
    return " ".join(texto.split()).upper()


# ─────────────────────────────────────────── Normalização para *matching*
#
# Diferente de `normalizar_historico_contabil` (que formata texto para
# *exibição* e preserva acentos), o que segue existe só para *comparar*
# histórico de extrato com histórico de regra. Nada disto é persistido no
# lugar do texto original — o extrato continua intacto para auditoria.
#
# Motivação (feedback do escritório, 2026-08-18): uma regra cadastrada como
# "TARIFA" precisa reconhecer "TARIFA COM LIQUIDAÇÃO" e "TARIFA COM R
# LIQUIDAÇÃO". O banco escreve o mesmo histórico com acento ou sem, com
# espaço duplo, com hífen ou barra no meio — e o contador digita a regra
# do jeito dele. Comparar os dois lados nesta forma canônica elimina essa
# classe inteira de "regra não pegou".


def remover_acentos(texto: str) -> str:
    """Remove diacríticos: 'LIQUIDAÇÃO' → 'LIQUIDACAO'."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def normalizar_para_match(texto: str) -> str:
    """Forma canônica de comparação: minúsculas, sem acento, sem pontuação,
    espaços colapsados.

    'TARIFA  COM/LIQUIDAÇÃO' e 'tarifa com liquidacao' viram a mesma coisa.
    Pontuação vira espaço (não some) para que 'TARIFA-BANCARIA' continue
    sendo duas palavras e não 'tarifabancaria'.
    """
    sem_acento = remover_acentos(texto).lower()
    limpo = _NAO_ALFANUMERICO.sub(" ", sem_acento)
    return " ".join(limpo.split())


def tokens_para_match(texto: str) -> list[str]:
    """Palavras da forma canônica, na ordem em que aparecem."""
    return normalizar_para_match(texto).split()
