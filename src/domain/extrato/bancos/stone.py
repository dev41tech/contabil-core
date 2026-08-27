"""Extrato da Stone — sinal na coluna TIPO e lançamentos do mais recente ao mais antigo.

O layout é:

    DATA        TIPO     LANÇAMENTO        VALOR (R$)  SALDO (R$)  CONTRAPARTE
                         CALPIE PINTURAS
    31/07/2025  Débito   INDUSTRIAIS LTDA      226,00       64,73
                         Transferência | Pix

Três coisas que nenhum parser genérico acerta neste arquivo:

1. **O valor não tem sinal.** `226,00` é débito porque a coluna TIPO diz
   "Débito". Lido sem isso, todo lançamento vira crédito e o extrato fecha com
   o dobro do saldo.

2. **A ordem é decrescente** — 31/07 primeiro, 01/07 por último. A cadeia de
   saldos só fecha lendo de baixo para cima, e `ordem` (que desempata
   lançamentos do mesmo dia na tela e no arquivo exportado) sairia invertida.
   Quem devolve a ordem é `ordenar_do_mais_antigo`.

3. **O nome da contraparte quebra em volta da linha de dados** — parte acima,
   parte na própria linha, e abaixo vem o meio de pagamento
   ("Transferência | Pix"). Os fragmentos ficam na MESMA coluna do resto da
   descrição, então não há como separá-los por posição horizontal; o que separa
   é a distância vertical (±12 pontos dentro do grupo contra 48 entre grupos).
   Por isso este adaptador lê por coordenada.

O extrato não traz `SALDO ANTERIOR` nem `SALDO FINAL`: a conferência é a cadeia
entre lançamentos, que a Stone imprime em todas as linhas.
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
    colar_fragmentos,
    gerar_fitid,
    ordenar_do_mais_antigo,
    parse_data,
    parse_valor,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"STONE", "197"})

_VALOR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
_DATA_LONGA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TOLERANCIA_COLUNA = 16.0

# A coluna TIPO é a fonte do sinal.
_DEBITO = ("débito", "debito")
_CREDITO = ("crédito", "credito")

_ASSINATURA = re.compile(
    r"\bdata\b.*\btipo\b.*\blan[çc]amento\b.*\bvalor\b.*\bsaldo\b", re.IGNORECASE
)


class _Colunas:
    __slots__ = ("x_data", "x_tipo", "x_lancamento", "valor", "saldo")

    def __init__(self, x_data: float, x_tipo: float, x_lancamento: float,
                 valor: float, saldo: float) -> None:
        self.x_data = x_data
        self.x_tipo = x_tipo
        self.x_lancamento = x_lancamento
        self.valor = valor
        self.saldo = saldo


def _ler_cabecalho(linhas: list[list[dict]]) -> _Colunas | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if "data" not in textos or "tipo" not in textos:
            continue
        if not any(t.startswith("lan") for t in textos):
            continue
        valor = borda_direita(textos, palavras, "valor")
        saldo = borda_direita(textos, palavras, "saldo")
        if valor is None or saldo is None:
            continue
        return _Colunas(
            x_data=float(palavras[textos.index("data")]["x0"]),
            x_tipo=float(palavras[textos.index("tipo")]["x0"]),
            x_lancamento=float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("lan"))]["x0"]
            ),
            valor=valor,
            saldo=saldo,
        )
    return None


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)



def _saldo_so_no_fim_do_grupo(
    transacoes: list[TransacaoOFX],
) -> list[TransacaoOFX]:
    """Tira o saldo das linhas que só repetem o saldo da linha seguinte.

    Quando um Pix vem com tarifa, a Stone imprime as duas linhas com o MESMO
    saldo — o do par já líquido. Exemplo real: saldo anterior 1.363,28, crédito
    de 126,99, tarifa de 0,95, e as duas linhas mostram 1.489,32. Lido como
    saldo por linha, o crédito sozinho teria de levar a 1.490,27 e a cadeia
    quebra por exatamente o valor da tarifa.

    Saldo igual em duas linhas seguidas só é possível se a do meio valesse zero
    — e lançamento de valor zero é descartado antes daqui. Ou seja: saldo
    repetido é sempre fecho de grupo, nunca saldo da linha. Mantendo-o apenas na
    última do grupo, a conferência por segmentos do validador soma o par inteiro
    e fecha.
    """
    resultado = list(transacoes)
    for i in range(len(resultado) - 1):
        atual, seguinte = resultado[i], resultado[i + 1]
        if (
            atual.saldo_apos is not None
            and atual.saldo_apos == seguinte.saldo_apos
        ):
            resultado[i] = replace(atual, saldo_apos=None)
    return resultado


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    idx = 0
    # As colunas valem até a próxima página que traga cabeçalho próprio: nem
    # todo extrato repete o cabeçalho em toda página, e pular a página inteira
    # perderia os lançamentos dela.
    colunas: _Colunas | None = None

    for palavras in paginas:
        linhas = agrupar_linhas(palavras)
        colunas = _ler_cabecalho(linhas) or colunas
        if colunas is None:
            continue

        # Nada acima do cabeçalho da tabela é contraparte: ali estão o titular,
        # o CNPJ e o período, que colados no primeiro lançamento fariam do
        # próprio correntista a contraparte dele.
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
            sinal = 0
            descricao: list[str] = []
            valor: Decimal | None = None
            saldo: Decimal | None = None

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA_LONGA.match(texto) and abs(x0 - colunas.x_data) <= 14:
                    data_lida = parse_data(texto, referencia_ano)
                    continue

                if abs(x0 - colunas.x_tipo) <= 8:
                    baixa = texto.lower()
                    if baixa in _DEBITO:
                        sinal = -1
                        continue
                    if baixa in _CREDITO:
                        sinal = 1
                        continue

                if _VALOR.match(texto):
                    convertido = parse_valor(texto)
                    if convertido is not None:
                        if abs(x1 - colunas.saldo) <= _TOLERANCIA_COLUNA:
                            saldo = convertido
                            continue
                        if abs(x1 - colunas.valor) <= _TOLERANCIA_COLUNA:
                            valor = convertido
                            continue

                if x0 >= colunas.x_lancamento - 3:
                    descricao.append(texto)

            texto_descricao = re.sub(r"\s+", " ", " ".join(descricao)).strip()

            if data_lida is None or valor is None or sinal == 0:
                if texto_descricao:
                    fragmentos.append((topo, texto_descricao))
                continue

            valor = abs(valor) * sinal
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
        t if t.historico else replace(t, historico="SEM DESCRIÇÃO")
        for t in transacoes
    ]
    cronologicas = ordenar_do_mais_antigo(transacoes)
    return [Bloco(transacoes=_saldo_so_no_fim_do_grupo(cronologicas))]
