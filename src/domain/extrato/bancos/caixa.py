"""Extrato da Caixa — cada lançamento é uma linha visual partida em três.

    Data/Hora   Nr. Doc.  Descrição/Detalhamento          Valor (R$)   Saldo(R$)
    04/08/2025            DEB PIX CHAVE
                41436                                   3.797,58 D    592,08 D
    14:36:09              XXX.794.898-XX FULANO DE TAL

As três alturas acima são **um lançamento só**. O pdfplumber as devolve como
linhas separadas porque estão a ~4,6 pontos uma da outra; o lançamento seguinte
fica a ~25. Com a altura padrão de agrupamento (3 pontos), nenhuma das três tem
data, valor e saldo ao mesmo tempo — e por isso nada casava. Aqui o agrupamento
usa 12 pontos, que junta as três e ainda separa do lançamento de baixo.

Duas outras coisas:

- **O sinal é uma letra separada do número** (`3.797,58` e `D` são palavras
  distintas, a letra ~6 pontos à direita). Cada valor procura a sua letra à
  direita; sem ela o lançamento é descartado, porque um valor da Caixa sem D/C
  não tem como ser interpretado.
- **A data e a hora ocupam a MESMA coluna** (`04/08/2025` e `14:36:09`, ambas
  em x≈25). A hora é ignorada: o extrato é conciliado por dia, e `Transacao`
  guarda data de calendário sem fuso.

`SALDO DIA` fecha o dia e não é lançamento — o saldo dele repete o da última
linha do dia, então não acrescenta âncora nenhuma.

**Linha que não move o saldo não é movimento — e ignorá-la evita dobrar valor.**

A Caixa lança cheque depositado DUAS vezes:

    13/08  DEPOSITO CHEQUE ATM        5.110,00 C   saldo 0,00 C
    15/08  DESBLOQ CHEQUE DEPOSITADO  5.110,00 C   saldo 4.110,00 C

O primeiro credita e o saldo não anda: o dinheiro está depositado, não
disponível. O segundo é a liberação, e é aí que o saldo se move. Importar os
dois põe R$ 10.220,00 no razão onde entraram R$ 5.110,00.

A regra usada não olha a descrição, olha o saldo: **se o saldo impresso é igual
ao da linha anterior, aquela linha não movimentou a conta** e fica de fora. É a
mesma coluna que o banco usa para se conferir, e não depende de adivinhar quais
descrições são de bloqueio — que variam por produto. De quebra, é o que faz a
cadeia de saldos fechar: com a linha bloqueada dentro, ela acusa uma diferença
de 5.110,00 que não existe.
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

SIGLAS = frozenset({"CAIXA", "CEF", "104"})

# A Caixa parte o lançamento em três alturas a ~4,6 pontos; o seguinte vem a ~25.
_ALTURA_DO_LANCAMENTO = 12.0
_TOLERANCIA_COLUNA = 16.0
# Distância máxima entre o número e a letra D/C que lhe dá o sinal.
_DISTANCIA_DA_LETRA = 22.0

_VALOR = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")
_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_HORA = re.compile(r"^\d{2}:\d{2}:\d{2}$")

_ASSINATURA = re.compile(
    r"\bdata/hora\b.*\bdoc\b.*\bdescri[çc][ãa]o.*\bvalor\b.*\bsaldo\b",
    re.IGNORECASE,
)


class _Colunas:
    __slots__ = ("x_data", "x_descricao", "valor", "saldo")

    def __init__(self, x_data: float, x_descricao: float,
                 valor: float, saldo: float) -> None:
        self.x_data = x_data
        self.x_descricao = x_descricao
        self.valor = valor
        self.saldo = saldo

    def qual(self, x1: float) -> str | None:
        melhor, distancia = None, _TOLERANCIA_COLUNA
        for nome, borda in (("valor", self.valor), ("saldo", self.saldo)):
            d = abs(x1 - borda)
            if d < distancia:
                melhor, distancia = nome, d
        return melhor


def _ler_cabecalho(linhas: list[list[dict]]) -> _Colunas | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if not any(t.startswith("data/hora") for t in textos):
            continue
        if not any(t.startswith("descri") for t in textos):
            continue
        valor = borda_direita(textos, palavras, "valor")
        saldo = borda_direita(textos, palavras, "saldo")
        if valor is None or saldo is None:
            continue
        return _Colunas(
            x_data=float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("data/hora"))]["x0"]
            ),
            x_descricao=float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("descri"))]["x0"]
            ),
            valor=valor,
            saldo=saldo,
        )
    return None


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def _sinal_a_direita(x1: float, letras: list[tuple[float, str]]) -> str | None:
    """A letra D/C mais próxima à direita do número, dentro do alcance."""
    candidatas = [
        (x0 - x1, letra) for x0, letra in letras if 0 <= x0 - x1 <= _DISTANCIA_DA_LETRA
    ]
    if not candidatas:
        return None
    return min(candidatas)[1]


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    idx = 0
    colunas: _Colunas | None = None
    ultimo_saldo: Decimal | None = None

    for palavras in paginas:
        linhas = agrupar_linhas(palavras, altura=_ALTURA_DO_LANCAMENTO)
        colunas = _ler_cabecalho(linhas) or colunas
        if colunas is None:
            continue

        for linha in linhas:
            if _ler_cabecalho([linha]) is not None:
                continue

            data_lida: date | None = None
            descricao: list[str] = []
            numeros: list[tuple[float, str, Decimal]] = []
            letras: list[tuple[float, str]] = []

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA.match(texto) and abs(x0 - colunas.x_data) <= 10:
                    data_lida = parse_data(texto, referencia_ano)
                    continue
                if _HORA.match(texto):
                    # Mesma coluna da data; o extrato é conciliado por dia.
                    continue
                if texto.upper() in ("D", "C") and len(texto) == 1:
                    letras.append((x0, texto.upper()))
                    continue
                if _VALOR.match(texto):
                    coluna = colunas.qual(x1)
                    convertido = parse_valor(texto)
                    if coluna and convertido is not None:
                        numeros.append((x1, coluna, convertido))
                        continue
                if x0 >= colunas.x_descricao - 3:
                    descricao.append(texto)

            texto_descricao = re.sub(r"\s+", " ", " ".join(descricao)).strip()
            if texto_descricao.upper().startswith("SALDO DIA"):
                # Fecha o dia repetindo o saldo da última linha: não é
                # lançamento e não acrescenta âncora.
                continue

            valor = saldo = None
            for x1, coluna, bruto in numeros:
                letra = _sinal_a_direita(x1, letras)
                if letra is None:
                    continue
                com_sinal = -abs(bruto) if letra == "D" else abs(bruto)
                if coluna == "valor":
                    valor = com_sinal
                else:
                    saldo = com_sinal

            if valor is None or valor == 0 or data_lida is None:
                continue

            # Linha que não moveu o saldo: dinheiro depositado e ainda não
            # liberado. Ele volta como lançamento próprio quando desbloqueia —
            # importar os dois dobraria o valor. Ver a nota no topo do módulo.
            if saldo is not None and saldo == ultimo_saldo:
                continue
            if saldo is not None:
                ultimo_saldo = saldo

            historico = texto_descricao[:200] or "SEM DESCRIÇÃO"
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(data_lida, historico, valor, idx),
                    data=data_lida,
                    valor=valor,
                    historico=historico,
                    tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                    saldo_apos=saldo,
                    ordem=idx,
                )
            )
            idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes)]
