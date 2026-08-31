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
from dataclasses import replace
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


# O "Gerenciador CAIXA" não tem `Data/Hora`: as duas datas viram colunas
# próprias e o cabeçalho quebra em alturas. O que sobra numa linha só são as
# quatro colunas do meio — e `Valor(R$)`/`Saldo(R$)` sem espaço é marca dele.
_ASSINATURA_GERENCIADOR = re.compile(
    r"\bdocumento\b.*\bhist[óo]rico\b.*\bvalor\(r\$\).*\bsaldo\(r\$\)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(
        _ASSINATURA.search(linha) or _ASSINATURA_GERENCIADOR.search(linha)
        for linha in linhas
    )


def _sinal_a_direita(x1: float, letras: list[tuple[float, str]]) -> str | None:
    """A letra D/C mais próxima à direita do número, dentro do alcance."""
    candidatas = [
        (x0 - x1, letra) for x0, letra in letras if 0 <= x0 - x1 <= _DISTANCIA_DA_LETRA
    ]
    if not candidatas:
        return None
    return min(candidatas)[1]


# ──────────────────────────────────────────── layout "Gerenciador CAIXA"
#
# Exportação tabular, sem a letra D/C que o outro layout usa:
#
#     Data de      Data de                                    Valor(R$)   Saldo(R$)
#     lançamento   movimento   Documento  Histórico
#     02/02/2026   02/02/2026  0          DEBITO DE IOF        - 197,96   R$ 63.725,71
#     02/02/2026   02/02/2026  0          COBRANCA DE JUROS  - 9.710,68   R$ 73.436,39
#     02/02/2026   02/02/2026  0          SALDO DIA                0,00   R$ 73.436,39
#
# Três coisas mudam em relação ao layout com D/C:
#
#   - o sinal é um `-` SOLTO na coluna de valor, e não uma letra à direita;
#   - a célula quebra: `MENSALIDADE CESTA` / `SERVICO` em duas alturas, e um
#     valor cujo `-` ficou numa altura e o `11.178,16` na de baixo;
#   - **o saldo não traz sinal nenhum.** Ver `_orientar_saldos`.

_ALTURA_DO_GERENCIADOR = 14.0
_TOLERANCIA_CADEIA = Decimal("0.05")
_MENOS = re.compile(r"^[-–—]$")


def _ler_cabecalho_gerenciador(linhas: list[list[dict]]) -> _Colunas | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if "documento" not in textos or not any(t.startswith("hist") for t in textos):
            continue
        if "data" not in textos:
            continue
        valor = borda_direita(textos, palavras, "valor")
        saldo = borda_direita(textos, palavras, "saldo")
        if valor is None or saldo is None:
            continue
        return _Colunas(
            # Duas colunas de data, lançamento e movimento. A primeira é a de
            # lançamento, que é a que o razão usa.
            x_data=float(palavras[textos.index("data")]["x0"]),
            x_descricao=float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("hist"))]["x0"]
            ),
            valor=valor,
            saldo=saldo,
        )
    return None


def _cadeia_fecha(transacoes: list[TransacaoOFX]) -> bool:
    """Os saldos impressos caminham com os valores, de âncora a âncora?"""
    anterior: Decimal | None = None
    acumulado = Decimal("0")
    viu_par = False
    for transacao in transacoes:
        acumulado += transacao.valor
        if transacao.saldo_apos is None:
            continue
        if anterior is not None:
            viu_par = True
            if abs((transacao.saldo_apos - anterior) - acumulado) > _TOLERANCIA_CADEIA:
                return False
        anterior = transacao.saldo_apos
        acumulado = Decimal("0")
    return viu_par


def _orientar_saldos(transacoes: list[TransacaoOFX]) -> list[TransacaoOFX]:
    """Descobre o SINAL do saldo, que este extrato não imprime.

    A conta está no vermelho e a CAIXA imprime o saldo devedor em módulo, sem
    `D` e sem menos:

        DEBITO DE IOF        - 197,96   R$ 63.725,71
        COBRANCA DE JUROS  - 9.710,68   R$ 73.436,39

    Um débito de 9.710,68 fez o número CRESCER. Isso é impossível num saldo
    credor — e é exatamente o que se espera de um devedor impresso sem sinal.

    Importar como está poria +R$ 84.951,75 no razão de uma conta que deve esse
    tanto: erro de sinal em saldo, que é a família do defeito que já pôs saldo no
    lugar de valor neste módulo.

    A decisão não é por heurística de descrição nem por chute: as duas leituras
    são TESTADAS contra a cadeia, e vale a que caminha. Se as duas caminham ou
    nenhuma caminha, nada é invertido e a conferência lá na frente recusa — não
    se escolhe sinal no empate.
    """
    if _cadeia_fecha(transacoes):
        return transacoes
    invertidas = [
        replace(t, saldo_apos=-t.saldo_apos if t.saldo_apos is not None else None)
        for t in transacoes
    ]
    return invertidas if _cadeia_fecha(invertidas) else transacoes


def _extrair_gerenciador(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    idx = 0
    colunas: _Colunas | None = None

    for palavras in paginas:
        linhas = agrupar_linhas(palavras, altura=_ALTURA_DO_GERENCIADOR)
        colunas = _ler_cabecalho_gerenciador(linhas) or colunas
        if colunas is None:
            continue

        for linha in linhas:
            if _ler_cabecalho_gerenciador([linha]) is not None:
                continue

            data_lida: date | None = None
            # (altura, x0, texto): a célula de histórico quebra em duas alturas
            # — `MENSALIDADE CESTA` em cima, `SERVICO` embaixo. Ordenar só pelo
            # x0 embaralha as duas metades e sai "MENSALIDADE SERVICO CESTA".
            descricao: list[tuple[float, float, str]] = []
            valor: Decimal | None = None
            saldo: Decimal | None = None
            negativo = False

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA.match(texto):
                    # Só a PRIMEIRA coluna de data; a de movimento é ignorada.
                    if abs(x0 - colunas.x_data) <= 8 and data_lida is None:
                        data_lida = parse_data(texto, referencia_ano)
                    continue
                if _MENOS.match(texto) and x1 >= colunas.valor - 60:
                    # O `-` mora na coluna de valor, à esquerda do número ou —
                    # quando a célula quebra — numa altura acima dele.
                    negativo = True
                    continue
                if _VALOR.match(texto):
                    convertido = parse_valor(texto)
                    if convertido is None:
                        continue
                    if abs(x1 - colunas.valor) <= _TOLERANCIA_COLUNA:
                        valor = abs(convertido)
                    elif abs(x1 - colunas.saldo) <= _TOLERANCIA_COLUNA:
                        saldo = convertido
                    continue
                if x0 >= colunas.x_descricao - 3 and x0 < colunas.valor - 40:
                    descricao.append((float(palavra["top"]), x0, texto))

            texto_descricao = re.sub(
                r"\s+", " ", " ".join(t for _, _, t in sorted(descricao))
            ).strip()
            if data_lida is None or valor is None:
                continue
            # `SALDO DIA` fecha o dia com valor 0,00: não é lançamento, e o
            # saldo dele só repete o da última linha do dia.
            if valor == 0 or texto_descricao.upper().startswith("SALDO DIA"):
                continue

            movimento = -valor if negativo else valor
            historico = texto_descricao[:200] or "SEM DESCRIÇÃO"
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(data_lida, historico, movimento, idx),
                    data=data_lida,
                    valor=movimento,
                    historico=historico,
                    tipo_ofx="CREDIT" if movimento > 0 else "DEBIT",
                    saldo_apos=saldo,
                    ordem=idx,
                )
            )
            idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=_orientar_saldos(transacoes))]


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    for palavras in paginas:
        if _ler_cabecalho_gerenciador(
            agrupar_linhas(palavras, altura=_ALTURA_DO_GERENCIADOR)
        ) is not None:
            return _extrair_gerenciador(paginas, referencia_ano)

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
