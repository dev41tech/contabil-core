"""Extrato do Mercado Pago — descrição quebrada acima e abaixo da linha de dados.

O layout é:

    Data        Descrição                    ID da operação    Valor       Saldo
                Pix recebido 36.576.422
    01-04-2026  FULANO ELIZIARIO LOPES DA    152067851233      R$ 238,08   R$ 372,35
                SILVA
                Dinheiro reservado Salário
    01-04-2026                               152073823577     -R$ 190,50   R$ 181,85
                Gustavo

O problema é a descrição: ela começa uma linha ACIMA da linha de dados e termina
uma linha ABAIXO, e entre dois lançamentos ficam duas linhas soltas — o fim da
descrição do primeiro e o começo da do segundo. No texto achatado não há como
saber onde uma acaba e a outra começa; por isso este adaptador lê por
coordenada. A quebra fica a ~12 pontos da linha de dados e o lançamento seguinte
a ~41, então a vizinhança resolve sem empate.

A data vem com hífen (`01-04-2026`), o que o `parse_data` já aceita. Os valores
trazem o sinal (`-R$ 190,50`) e toda linha traz saldo, então a cadeia fecha
lançamento a lançamento.

**A capa não é usada como âncora de propósito.** Ela imprime "Saldo inicial" e
"Saldo final", mas na ordem de leitura do PDF os rótulos saem DEPOIS dos
números (`R$ 134,27 R$ 130,51 Saldo inicial: Saldo final:`), e casar rótulo com
valor por proximidade textual pegava o total de saídas no lugar do saldo final.
Âncora errada é pior que âncora nenhuma: ela reprova extração correta. A cadeia
por lançamento já cobre tudo menos o primeiro.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import (
    Bloco,
    agrupar_linhas,
    colar_fragmentos,
    gerar_fitid,
    parse_data,
    parse_valor,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"MERCADOPAGO", "MERCADO PAGO", "323"})

_VALOR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
_DATA = re.compile(r"^\d{2}-\d{2}-\d{4}$")


_ASSINATURA = re.compile(
    r"\bdata\b.*\bdescri[çc][ãa]o\b.*\bid da opera[çc][ãa]o\b.*\bvalor\b.*\bsaldo\b",
    re.IGNORECASE,
)


class _Colunas:
    __slots__ = ("x_data", "x_descricao", "x_id", "valor", "saldo")

    def __init__(self, x_data: float, x_descricao: float, x_id: float,
                 valor: float, saldo: float) -> None:
        self.x_data = x_data
        self.x_descricao = x_descricao
        self.x_id = x_id
        self.valor = valor
        self.saldo = saldo


def _ler_cabecalho(linhas: list[list[dict]]) -> _Colunas | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if "data" not in textos or "valor" not in textos or "saldo" not in textos:
            continue
        if not any(t.startswith("descri") for t in textos):
            continue
        return _Colunas(
            x_data=float(palavras[textos.index("data")]["x0"]),
            x_descricao=float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("descri"))]["x0"]
            ),
            # A coluna "ID da operação" fica entre a descrição e o valor. É um
            # identificador interno do Mercado Pago, não a contraparte — deixá-lo
            # entrar no histórico só empurra o nome para fora do limite de 200
            # caracteres.
            x_id=float(palavras[textos.index("id")]["x0"]) if "id" in textos else 1e9,
            # Os rótulos "Valor" e "Saldo" são o começo das colunas; os números
            # são alinhados à direita e caem bem depois deles. A borda usada é a
            # do próprio número da primeira linha de dados, calibrada abaixo.
            valor=float(palavras[textos.index("valor")]["x1"]),
            saldo=float(palavras[textos.index("saldo")]["x1"]),
        )
    return None


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    idx = 0
    # O cabeçalho da tabela não se repete em toda página: das 21 do extrato
    # medido, várias seguem a listagem sem ele. Pular a página inteira quando
    # falta o cabeçalho perdeu dois dias de lançamentos entre a página 14 e a
    # 17 — e a cadeia de saldos acusou, como devia. As colunas valem até a
    # próxima página que traga um cabeçalho próprio.
    colunas: _Colunas | None = None

    for palavras in paginas:
        linhas = agrupar_linhas(palavras)
        colunas = _ler_cabecalho(linhas) or colunas
        if colunas is None:
            continue

        topo_cabecalho = min(
            (
                float(linha[0]["top"])
                for linha in linhas
                if _ler_cabecalho([linha]) is not None
            ),
            default=0.0,
        )

        ancoras: list[tuple[float, int]] = []
        fragmentos: list[tuple[float, str]] = []

        for linha in linhas:
            topo = float(linha[0]["top"])
            if topo <= topo_cabecalho:
                continue

            data_lida: date | None = None
            descricao: list[str] = []
            numeros: list[tuple[float, Decimal]] = []

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA.match(texto) and abs(x0 - colunas.x_data) <= 8:
                    data_lida = parse_data(texto, referencia_ano)
                    continue
                if texto in ("R$", "$"):
                    continue
                if _VALOR.match(texto):
                    convertido = parse_valor(texto)
                    if convertido is not None:
                        numeros.append((x1, convertido))
                    continue
                if colunas.x_descricao - 3 <= x0 < colunas.x_id - 3:
                    descricao.append(texto)

            texto_descricao = re.sub(r"\s+", " ", " ".join(descricao)).strip()

            # Linha de dados: tem data e os dois números (valor e saldo), sempre
            # nessa ordem da esquerda para a direita.
            if data_lida is None or len(numeros) < 2:
                if texto_descricao:
                    fragmentos.append((topo, texto_descricao))
                continue

            numeros.sort(key=lambda par: par[0])
            valor, saldo = numeros[-2][1], numeros[-1][1]
            if valor == 0:
                continue

            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(data_lida, texto_descricao, valor, idx),
                    data=data_lida,
                    valor=valor,
                    historico=texto_descricao[:200],
                    tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                    saldo_apos=saldo,
                    ordem=idx,
                )
            )
            ancoras.append((topo, len(transacoes) - 1))
            idx += 1

        transacoes = colar_fragmentos(transacoes, ancoras, fragmentos)

    if not transacoes:
        return []
    transacoes = [
        t if t.historico else replace(t, historico="SEM DESCRIÇÃO") for t in transacoes
    ]
    return [Bloco(transacoes=transacoes)]



