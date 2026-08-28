"""Primitivas compartilhadas entre o parser genérico e os adaptadores por banco.

Estavam dentro de `pdf_parser.py`. Saíram para cá quando os adaptadores por banco
passaram a precisar delas: `pdf_parser` importa o registro de adaptadores, então
qualquer adaptador que importasse `pdf_parser` fecharia um ciclo. Este módulo não
importa nada de dentro de `extrato` além do `TransacaoOFX`, e é o que quebra o
ciclo.

`pdf_parser` reexporta os nomes antigos (`_parse_valor`, `_parse_data`,
`_gerar_fitid`) para não mexer em quem já os importava.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from src.domain.extrato.ofx_parser import TransacaoOFX

_DATE_FULL_RE = re.compile(r"\b(\d{2})[/\-](\d{2})[/\-](\d{2,4})\b")
_DATE_SHORT_RE = re.compile(r"\b(\d{2})[/\-](\d{2})\b")

# Valor monetário brasileiro, com ou sem "R$", com sinal antes ou depois.
# O sinal DEPOIS do número é o padrão do Itaú ("4.026,15-") e não existia aqui.
MOEDA_RE = re.compile(r"-?R?\$?\s?\d{1,3}(?:\.\d{3})*,\d{2}-?")

MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


def parse_data(s: str, referencia_ano: int | None = None) -> date | None:
    """Aceita DD/MM/AAAA, DD/MM/AA ou DD/MM (sem ano).

    Devolve data de calendário: é literalmente o que está impresso na linha do
    extrato, sem hora e sem fuso para um consumidor reinterpretar.
    """
    s = s.strip()
    m = _DATE_FULL_RE.search(s)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        try:
            return date(int(y), int(mo), int(d))
        except ValueError:
            pass
    m2 = _DATE_SHORT_RE.search(s)
    if m2:
        d, mo = m2.group(1), m2.group(2)
        ano = referencia_ano or datetime.now(UTC).year
        try:
            return date(ano, int(mo), int(d))
        except ValueError:
            pass
    return None


_DATA_EXTENSO_RE = re.compile(
    r"\b(\d{1,2})\s+de\s+([A-Za-zçÇãÃéÉ]+)\s+de\s+(\d{4})\b", re.IGNORECASE
)


def parse_data_extenso(s: str) -> date | None:
    """Aceita "2 de Janeiro de 2026" — o cabeçalho de dia do Inter.

    Separada de `parse_data` de propósito: só o Inter escreve a data assim, e
    misturar as duas faria uma linha de texto qualquer que contenha "de ... de"
    virar data no parser de outro banco.
    """
    m = _DATA_EXTENSO_RE.search(s.strip())
    if not m:
        return None
    mes = MESES_PT.get(m.group(2).lower())
    if not mes:
        return None
    try:
        return date(int(m.group(3)), mes, int(m.group(1)))
    except ValueError:
        return None


def parse_valor(s: str) -> Decimal | None:
    """Converte um valor impresso em Decimal, com o sinal que estiver nele.

    Aceita sinal à esquerda ("-1.234,56"), entre parênteses ("(1.234,56)") e à
    direita ("1.234,56-", padrão Itaú para débito).
    """
    s = s.strip().replace("R$", "").replace(" ", "").replace("\xa0", "")
    negative = s.startswith("-") or s.startswith("(") or s.endswith("-")
    s = s.lstrip("-(").rstrip(")-")
    if re.match(r"^\d{1,3}(,\d{3})*\.\d{2}$", s):
        val = Decimal(s.replace(",", ""))
    elif "," in s and "." in s:
        val = Decimal(s.replace(".", "").replace(",", "."))
    elif "," in s:
        val = Decimal(s.replace(",", "."))
    else:
        try:
            val = Decimal(s)
        except InvalidOperation:
            return None
    return -val if negative else val


def gerar_fitid(data: date, historico: str, valor: Decimal, idx: int) -> str:
    raw = f"{data.isoformat()}{historico}{valor}{idx}"
    return "PDF" + hashlib.md5(raw.encode()).hexdigest()[:12].upper()


@dataclass(frozen=True)
class Bloco:
    """Um trecho de extrato com cadeia de saldos própria.

    Um PDF pode trazer mais de um: o Bradesco emite "Extrato" (o mês fechado) e
    "Últimos Lançamentos" (os dias seguintes) no mesmo arquivo, cada um abrindo
    com seu próprio `SALDO ANTERIOR`. Conferir os dois como uma cadeia só acusa
    um salto no ponto de emenda que não é erro nenhum — foi o que reprovou o
    Bradesco inteiro mesmo com os 119 lançamentos corretos.

    `saldo_anterior` e `saldo_final` são os saldos impressos na abertura e no
    fecho do bloco, quando existem. Eles conferem as duas pontas que uma cadeia
    solta deixa de fora: o PRIMEIRO lançamento, que não tem antecessor, e a
    CAUDA depois do último lançamento que traz saldo — que no Itaú são seis
    linhas, e foi exatamente onde entrou uma linha de rodapé lida como
    lançamento de R$ 19.070,30.
    """

    transacoes: list[TransacaoOFX]
    saldo_anterior: Decimal | None = None
    saldo_final: Decimal | None = None

# Altura, em pontos, dentro da qual duas palavras são da mesma linha.
ALTURA_LINHA = 3.0


def agrupar_linhas(
    palavras: list[dict], altura: float = ALTURA_LINHA
) -> list[list[dict]]:
    """Agrupa palavras em linhas por proximidade vertical.

    Agrupar por faixa fixa (`int(top / altura)`) parte uma linha em duas sempre
    que as duas metades caem em faixas vizinhas — e elas caem: no Itaú a mesma
    linha visual traz palavras com `top` 415,4 e 415,6. A comparação é com o
    topo da linha em aberto, não com uma grade.

    `altura` é ajustável porque nem todo banco escreve a linha na mesma altura.
    A Caixa quebra CADA lançamento em três alturas — data, valores e hora ficam
    a ~4,6 pontos uma da outra, e o lançamento seguinte a ~25. Com o padrão de
    3 pontos, um lançamento vira três "linhas" e nenhuma delas tem data, valor e
    saldo ao mesmo tempo.
    """
    if not palavras:
        return []
    ordenadas = sorted(palavras, key=lambda w: (w["top"], w["x0"]))
    linhas: list[list[dict]] = [[ordenadas[0]]]
    topo = float(ordenadas[0]["top"])
    for palavra in ordenadas[1:]:
        if float(palavra["top"]) - topo > altura:
            linhas.append([])
            topo = float(palavra["top"])
        linhas[-1].append(palavra)
    return [sorted(linha, key=lambda w: w["x0"]) for linha in linhas]


def colar_fragmentos(
    transacoes: list[TransacaoOFX],
    ancoras: list[tuple[float, int]],
    fragmentos: list[tuple[float, str]],
    distancia_maxima: float = 20.0,
) -> list[TransacaoOFX]:
    """Cola cada fragmento de texto no lançamento verticalmente mais próximo.

    Dois bancos quebram o nome da contraparte em linhas soltas em volta da linha
    de dados, e nos dois o fragmento fica na MESMA coluna do resto da descrição
    — não há como separá-lo por posição horizontal. O que separa é a distância
    vertical: no Itaú a quebra fica a ~5 pontos da linha de dados e o lançamento
    seguinte a ~13; no Stone, a ~12 contra ~48. Em nenhum dos dois há empate.

    `ancoras` são pares (topo, posição em `transacoes`); `fragmentos`, pares
    (topo, texto).
    """
    if not ancoras:
        return transacoes
    resultado = list(transacoes)
    for topo, texto in fragmentos:
        vizinha = min(ancoras, key=lambda ancora: abs(ancora[0] - topo))
        if abs(vizinha[0] - topo) > distancia_maxima:
            # Texto longe de qualquer lançamento é rodapé de página, não
            # contraparte: o Stone imprime "Informações do Comprovante" e os
            # dados da ouvidoria depois do último lançamento.
            continue
        posicao = vizinha[1]
        alvo = resultado[posicao]
        # A ordem de leitura importa: o nome quebrado começa ACIMA da linha de
        # dados e termina nela ("CALPIE PINTURAS" / "INDUSTRIAIS LTDA"). Colar
        # tudo no fim devolvia "INDUSTRIAIS LTDA CALPIE PINTURAS", que nenhuma
        # busca por razão social encontra.
        if topo < vizinha[0]:
            juntado = f"{texto} {alvo.historico}".strip()[:200]
        else:
            juntado = f"{alvo.historico} {texto}".strip()[:200]
        resultado[posicao] = replace(alvo, historico=juntado)
    return resultado


def ordenar_do_mais_antigo(transacoes: list[TransacaoOFX]) -> list[TransacaoOFX]:
    """Inverte a lista quando o extrato foi emitido do mais recente para o mais antigo.

    Stone e Grafeno emitem assim. Não é detalhe de apresentação: `ordem` é a
    posição usada para desempatar lançamentos do mesmo dia na tela e no arquivo
    exportado (ver `ordenacao.py`), e a cadeia de saldos só fecha lendo do
    primeiro lançamento para o último. Com a lista invertida, as duas coisas
    saem trocadas.

    A inversão é decidida pelos dados, não pelo banco: se as datas já vêm
    crescentes, nada muda; se vêm decrescentes, inverte. Lista com datas
    embaralhadas fica como está — aí o problema não é a ordem.
    """
    if len(transacoes) < 2:
        return transacoes
    datas = [t.data for t in transacoes]
    if all(anterior <= seguinte for anterior, seguinte in zip(datas, datas[1:], strict=False)):
        return transacoes
    if all(anterior >= seguinte for anterior, seguinte in zip(datas, datas[1:], strict=False)):
        return list(reversed(transacoes))
    return transacoes


# Rótulos de moeda que acompanham o nome da coluna no cabeçalho.
SUFIXOS_MOEDA = ("r$", "rs", "(r$)")


def borda_direita(
    textos: list[str],
    palavras: list[dict],
    comeca_com: str,
    excluindo: tuple[str, ...] = (),
) -> float | None:
    """Borda direita da coluna cujo rótulo começa com `comeca_com`.

    As colunas numéricas são alinhadas à direita, então é a borda direita que
    identifica a coluna — o `x0` varia com a largura do número. Quando o rótulo
    vem seguido de "R$" ou "(R$)", quem marca a borda é esse sufixo.

    `excluindo` existe porque "saídas" e "saldo" começam iguais: sem ele, a
    busca por "sa" pode cair na coluna errada conforme a ordem do cabeçalho.
    """
    for i, texto in enumerate(textos):
        if not texto.startswith(comeca_com):
            continue
        if any(texto.startswith(proibido) for proibido in excluindo):
            continue
        seguinte = palavras[i + 1] if i + 1 < len(palavras) else None
        if seguinte is not None and seguinte["text"].lower() in SUFIXOS_MOEDA:
            return float(seguinte["x1"])
        return float(palavras[i]["x1"])
    return None
