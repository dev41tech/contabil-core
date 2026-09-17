"""Extrato de conta corrente do Sicredi — o relatório da cooperativa.

    DATA        DOCUMENTO   HISTORICO                          DEBITO   CREDITO    SALDO
    **/**/****  *********   S A L D O  A N T E R I O R                              0,00
    09/01/2024  PIX_DEB     PAGAMENTO PIX ... APARECIDO C      150,00
    09/01/2024  CAPTACAO    RESG.APLIC.FIN.AVISO PREV                   150,00      0,00

**Este NÃO é o único layout do Sicredi.** O outro é o mais comum na base do
escritório — 38 dos 63 arquivos:

    Data Descrição Documento Valor (R$) Saldo (R$)
    SALDO ANTERIOR 12.017,04
    01/12/2025 RECEBIMENTO PIX ... PIX_CRED 47,96 12.065,00
    01/12/2025 LIQUIDACAO BOLETO ... MELLODI 251066667 -351,26 12.725,35

Uma coluna de valor, com o sinal nele, e o saldo em toda linha. Ele era lido
pelo parser genérico, e 23 dos 38 arquivos eram RECUSADOS — ver
`_extrair_valor_e_saldo` para a causa, que não estava no formato das linhas.

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
        # O outro layout do banco. As palavras já estão na mão, então agrupá-las
        # de volta em linhas custa menos que uma segunda leitura do PDF.
        linhas_texto = [
            " ".join(palavra["text"] for palavra in linha)
            for palavras in paginas
            for linha in agrupar_linhas(palavras)
        ]
        return _extrair_valor_e_saldo(linhas_texto, referencia_ano)
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]


# ─────────────────────────────── Segundo layout: uma coluna de valor com sinal

# A descrição é OPCIONAL: quando o texto é longo, ele é quebrado inteiro em
# volta e a linha de dados fica só com data, valor e saldo —
# `13/10/2025 -407,50 1.546,50`. Exigir ao menos um caractere aqui descartava
# esses lançamentos, e o extrato passava a fechar 7 linhas a menos com a cadeia
# quebrada em 6 pontos.
_LINHA_VALOR_SALDO = re.compile(
    r"^(\d{2}/\d{2}/\d{4})\s+(.*?)\s*(-?\d{1,3}(?:\.\d{3})*,\d{2})"
    r"\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*$"
)
# Os dois rótulos de abertura vistos na base: "SALDO ANTERIOR" e "SALDO" seco.
_SALDO_ANTERIOR_SIMPLES = re.compile(
    r"^SALDO(?:\s+ANTERIOR)?\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*$", re.IGNORECASE
)

# O QUE RECUSAVA 23 DOS 38 ARQUIVOS DESTE LAYOUT
#
# O extrato não termina no último lançamento. Depois dele vem:
#
#     Lançamentos Futuros (Próximos 30 dias)
#     Data Descrição Valor (R$)
#     25/01/2026 CESTA EMPRESARIAL 02 -67,30
#
# São débitos AGENDADOS, que ainda não aconteceram, e a seção não tem coluna de
# saldo. O parser genérico os lia como movimento realizado: a cadeia de saldos
# quebrava e o arquivo inteiro era recusado.
#
# A recusa escondia o defeito pior. Se a cadeia não existisse, esses lançamentos
# entrariam no razão como se tivessem ocorrido — um débito futuro contabilizado
# hoje. É a mesma família da linha de rodapé do Itaú que entrou como crédito de
# R$ 19.070,30 em agosto, e a razão de o corte ser por marca explícita e não por
# heurística de "linha estranha no fim".
#
# Medido: dos arquivos deste layout que falhavam, TODOS tinham esta seção;
# nenhum dos que já liam tinha.
_FIM_DO_EXTRATO = re.compile(r"Lan[çc]amentos\s+Futuros", re.IGNORECASE)

_IGNORAR_VALOR_SALDO = re.compile(
    r"^(Data\s+Descri|Associado:|Cooperativa:|Conta:|Extrato\s*\(|"
    r"Valores das opera|Sicredi Fone|SAC\s|Ouvidoria|0800\s|\d{4}\s\d{4})",
    re.IGNORECASE,
)


def _extrair_valor_e_saldo(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    """Layout `Data Descrição Documento Valor (R$) Saldo (R$)`.

    Lido por texto, e não por coordenada, porque aqui há UMA coluna de valor: o
    sinal está no próprio número, então não é preciso saber em que coluna ele
    caiu. É o que separa este layout do outro deste mesmo banco.
    """
    limpas = [ln.strip() for ln in linhas]
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0

    # Linha solta já usada por um lançamento não pode ser usada por outro: duas
    # linhas de dados sem descrição em sequência disputariam o mesmo texto, e a
    # segunda roubaria a contraparte da primeira.
    consumidas: set[int] = set()

    def eh_estrutural(i: int) -> bool:
        if not 0 <= i < len(limpas) or not limpas[i]:
            return True
        return bool(
            _LINHA_VALOR_SALDO.match(limpas[i])
            or _SALDO_ANTERIOR_SIMPLES.match(limpas[i])
            or _IGNORAR_VALOR_SALDO.match(limpas[i])
            # O cabeçalho da seção de agendados fecha o extrato: sem isto ele
            # seria colado como se fosse o nome de uma contraparte.
            or _FIM_DO_EXTRATO.search(limpas[i])
        )

    def texto_solto(i: int) -> str:
        if eh_estrutural(i) or i in consumidas:
            return ""
        consumidas.add(i)
        return limpas[i]

    for i, limpa in enumerate(limpas):
        if not limpa:
            continue

        if _FIM_DO_EXTRATO.search(limpa):
            break

        if saldo_anterior is None:
            abertura = _SALDO_ANTERIOR_SIMPLES.match(limpa)
            if abertura:
                saldo_anterior = parse_valor(abertura.group(1))
                continue

        if _IGNORAR_VALOR_SALDO.match(limpa):
            continue

        casada = _LINHA_VALOR_SALDO.match(limpa)
        if not casada:
            continue

        data_str, meio, valor_str, saldo_str = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        valor = parse_valor(valor_str)
        saldo = parse_valor(saldo_str)
        if data_lida is None or valor is None or valor == 0:
            continue

        meio = re.sub(r"\s+", " ", meio).strip()
        # QUANDO COLAR O TEXTO DE VOLTA, E POR QUE NÃO SEMPRE
        #
        # Descrição longa é quebrada em volta da linha de dados, e o que sobra
        # no meio é só a coluna `Documento` — `PIX_DEB`, `CX240077` — ou nada:
        #
        #     PAGAMENTO PIX 00501462970 DOUGLAS ALEXANDRE
        #     01/10/2025 PIX_DEB -300,00 2.514,84
        #     DUFL
        #
        # `PIX_DEB` sozinho não identifica ninguém, e é justamente o nome da
        # contraparte que o NEO precisa para classificar.
        #
        # Mas colar SEMPRE erraria: o texto solto de uma quebra fica adjacente
        # também ao lançamento seguinte, que tem descrição própria e roubaria o
        # "DUFL" do vizinho. Duas ou mais palavras no meio significam descrição
        # completa na própria linha — aí não há o que colar.
        if len(meio.split()) < 2:
            acima = texto_solto(i - 1)
            abaixo = texto_solto(i + 1)
            # A ordem reproduz a da linha inteira: descrição e, no fim, o
            # documento — que é onde ele aparece quando tudo cabe numa linha só.
            historico = " ".join(p for p in (acima, abaixo, meio) if p).strip()
        else:
            historico = meio
        historico = re.sub(r"\s+", " ", historico)[:200] or "SEM DESCRIÇÃO"
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
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]
