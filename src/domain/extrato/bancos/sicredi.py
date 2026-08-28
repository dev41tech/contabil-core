"""Extrato de conta corrente do Sicredi — o relatório da cooperativa.

    DATA        DOCUMENTO   HISTORICO                          DEBITO   CREDITO    SALDO
    **/**/****  *********   S A L D O  A N T E R I O R                              0,00
    09/01/2024  PIX_DEB     PAGAMENTO PIX ... APARECIDO C      150,00
    09/01/2024  CAPTACAO    RESG.APLIC.FIN.AVISO PREV                   150,00      0,00

**Este NÃO é o único layout do Sicredi.** O outro — `Data Descrição Documento
Valor (R$) Saldo (R$)`, uma coluna de valor com sinal — é lido pelo parser
genérico e continua sendo. Quando o arquivo é daquele, `extrair_de_palavras`
aqui devolve lista vazia e o `parse_pdf` segue para o genérico sozinho. Por isso
este módulo pode reivindicar a sigla SICREDI sem tirar de funcionamento o que já
funcionava.

O que obriga a leitura por coordenada: **há três colunas numéricas** (`DEBITO`,
`CREDITO`, `SALDO`) e a maioria das linhas traz um número só. No texto achatado,
`09/01/2024 PIX_DEB PAGAMENTO PIX ... 150,00` e
`09/01/2024 CAPTACAO RESG.APLIC ... 150,00 0,00` são indistinguíveis quanto ao
sinal — o primeiro é débito e o segundo é crédito, e só a posição diz. As
colunas são alinhadas à direita e as bordas saem do próprio cabeçalho.

O saldo aparece só em algumas linhas, como no Itaú; a conferência por segmentos
do validador é que fecha a cadeia. A abertura vem na linha `S A L D O A N T E R
I O R`, com as letras separadas — por isso ela é reconhecida depois de remover
os espaços, e não por comparação literal.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import (
    Bloco,
    agrupar_linhas,
    borda_direita,
    gerar_fitid,
    parse_data,
    parse_valor,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"SICREDI", "748"})

_VALOR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TOLERANCIA_COLUNA = 14.0

_ASSINATURA = re.compile(
    r"\bdata\b.*\bdocumento\b.*\bhistorico\b.*\bdebito\b.*\bcredito\b.*\bsaldo\b",
    re.IGNORECASE,
)


class _Colunas:
    __slots__ = ("x_data", "x_documento", "debito", "credito", "saldo")

    def __init__(self, x_data: float, x_documento: float,
                 debito: float, credito: float, saldo: float) -> None:
        self.x_data = x_data
        self.x_documento = x_documento
        self.debito = debito
        self.credito = credito
        self.saldo = saldo

    def qual(self, x1: float) -> str | None:
        melhor, distancia = None, _TOLERANCIA_COLUNA
        for nome, borda in (
            ("debito", self.debito),
            ("credito", self.credito),
            ("saldo", self.saldo),
        ):
            d = abs(x1 - borda)
            if d < distancia:
                melhor, distancia = nome, d
        return melhor


def _ler_cabecalho(linhas: list[list[dict]]) -> _Colunas | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if not {"data", "documento", "historico", "debito", "credito", "saldo"} <= set(textos):
            continue
        debito = borda_direita(textos, palavras, "debito")
        credito = borda_direita(textos, palavras, "credito")
        saldo = borda_direita(textos, palavras, "saldo")
        if debito is None or credito is None or saldo is None:
            continue
        return _Colunas(
            x_data=float(palavras[textos.index("data")]["x0"]),
            x_documento=float(palavras[textos.index("documento")]["x0"]),
            debito=debito,
            credito=credito,
            saldo=saldo,
        )
    return None


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0
    colunas: _Colunas | None = None

    for palavras in paginas:
        linhas = agrupar_linhas(palavras)
        colunas = _ler_cabecalho(linhas) or colunas
        if colunas is None:
            continue

        for linha in linhas:
            data_lida: date | None = None
            descricao: list[str] = []
            valores: dict[str, Decimal] = {}

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA.match(texto) and abs(x0 - colunas.x_data) <= 14:
                    data_lida = parse_data(texto, referencia_ano)
                    continue
                if _VALOR.match(texto):
                    coluna = colunas.qual(x1)
                    if coluna:
                        convertido = parse_valor(texto)
                        if convertido is not None:
                            valores[coluna] = convertido
                        continue
                if x0 >= colunas.x_documento - 3:
                    descricao.append(texto)

            texto_descricao = " ".join(descricao).strip()
            # As letras vêm separadas ("S A L D O  A N T E R I O R").
            compacto = re.sub(r"[\s*]+", "", texto_descricao).upper()

            if compacto.startswith("SALDOANTERIOR"):
                if "saldo" in valores and saldo_anterior is None:
                    saldo_anterior = valores["saldo"]
                continue

            if "debito" in valores:
                valor = -abs(valores["debito"])
            elif "credito" in valores:
                valor = abs(valores["credito"])
            else:
                continue

            if valor == 0 or data_lida is None:
                continue

            historico = re.sub(r"\s+", " ", texto_descricao)[:200] or "SEM DESCRIÇÃO"
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(data_lida, historico, valor, idx),
                    data=data_lida,
                    valor=valor,
                    historico=historico,
                    tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                    saldo_apos=valores.get("saldo"),
                    ordem=idx,
                )
            )
            idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]
