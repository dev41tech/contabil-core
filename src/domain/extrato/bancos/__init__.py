"""Adaptadores de layout por banco.

Cada módulo aqui expõe três coisas:

    SIGLAS      frozenset[str]  — valores de `AgenciaBancaria.banco_sigla` que ele atende
    reconhece(linhas) -> bool   — a assinatura do layout, para conferência

e uma das duas formas de extrair:

    extrair(linhas, ano) -> list[Bloco]              a partir do texto por linha
    extrair_de_palavras(paginas, ano) -> list[Bloco] a partir das coordenadas

A segunda existe para layout de duas colunas, onde o texto já sai embaralhado
da extração e nenhuma regex desfaz isso depois — é o caso do Itaú. Custa uma
segunda passada no PDF, então só quem precisa a usa.

O despacho prefere a **sigla cadastrada na agência**: o banco é conhecido antes
de abrir o PDF, porque a importação já recebe `agencia_id`. O `reconhece` existe
para o caso de a sigla não estar preenchida e como conferência de que o arquivo
enviado é mesmo do banco cadastrado — arquivo trocado é erro comum de escritório.

Banco sem adaptador continua caindo no parser genérico e, depois dele, nas
camadas de IA. Nada aqui remove caminho: só acrescenta um determinístico à frente.
"""

from __future__ import annotations

from types import ModuleType

from src.domain.extrato.bancos import (
    bbc,
    bradesco,
    c6,
    cresol,
    fitbank,
    grafeno,
    inter,
    itau,
    mercadopago,
    nubank,
    santander,
    sicoob,
    sicredi,
    stone,
)

ADAPTADORES: tuple[ModuleType, ...] = (
    bbc,
    bradesco,
    c6,
    cresol,
    fitbank,
    grafeno,
    inter,
    itau,
    mercadopago,
    nubank,
    santander,
    sicoob,
    sicredi,
    stone,
)


def por_sigla(banco_sigla: str | None) -> ModuleType | None:
    if not banco_sigla:
        return None
    alvo = banco_sigla.strip().upper()
    for adaptador in ADAPTADORES:
        if alvo in adaptador.SIGLAS:
            return adaptador
    return None


def por_conteudo(linhas: list[str]) -> ModuleType | None:
    for adaptador in ADAPTADORES:
        if adaptador.reconhece(linhas):
            return adaptador
    return None


def escolher(banco_sigla: str | None, linhas: list[str]) -> ModuleType | None:
    """Adaptador para este arquivo, ou `None` se nenhum atende."""
    return por_sigla(banco_sigla) or por_conteudo(linhas)
