"""Extrato mensal do Itaú — a página tem duas colunas e o sinal está na coluna.

Este é o único adaptador que trabalha por **coordenada**, e não por linha de
texto. A razão é o layout: a página traz uma legenda lateral fixa

    A =agendamento
    B = ações movimentadas
    pelaBolsa de Valores
    C = crédito a compensar
    ...

à esquerda da tabela de lançamentos. O `extract_text` do pdfplumber achata a
página na ordem de leitura do PDF e intercala essa legenda no meio dos
lançamentos — "pelaBolsa de Valores 31/12 Saldo anterior 19.477,03-" sai como
uma linha só. Nenhuma regex separa depois o que a extração já juntou; por isso o
parser genérico devolvia zero para o Itaú.

Com a coordenada, duas coisas ficam simples:

1. **A legenda some** — é tudo que está à esquerda da coluna `data`.
2. **O sinal vem da coluna.** O Itaú tem colunas separadas de `entradas
   (créditos)` e `saídas (débitos)`, ambas alinhadas à direita. Um valor em
   `entradas` é crédito, em `saídas` é débito. O `-` no fim do número existe e
   concorda, mas a coluna é a fonte: ela não depende de o banco imprimir o sinal.

As bordas das colunas saem do próprio cabeçalho da tabela (`data descrição
entradas R$ saídas R$ saldo R$`), não de números fixos — o mesmo extrato sai com
larguras diferentes conforme o produto, e o cabeçalho é o que sempre acompanha.

**Linhas `SALDO ...` são ignoradas de propósito.** O Itaú imprime o saldo da
aplicação automática ("SALDO APLIC AUT MAIS") na mesma coluna de saldo da conta
corrente. Elas não são lançamento e — o que importa mais — não podem servir de
âncora da cadeia de saldos, porque o número é de outra conta. A única linha de
saldo aproveitada é a "Saldo anterior", que abre o extrato.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import (
    Bloco,
    colar_fragmentos,
    gerar_fitid,
    parse_data,
    parse_valor,
)
from src.domain.extrato._comum import (
    agrupar_linhas as _agrupar_linhas,
)
from src.domain.extrato._comum import (
    borda_direita as _borda_direita,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"ITAU", "ITAÚ", "341"})

# Um valor monetário do corpo da tabela, com o sinal opcional no fim.
_VALOR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}-?$")
_DATA_CURTA = re.compile(r"^\d{2}/\d{2}$")

# Tolerância, em pontos, para casar a borda direita de um valor com a da coluna.
# As colunas numéricas são alinhadas à direita, então a borda direita é estável
# enquanto o `x0` varia com a largura do número.
_TOLERANCIA_COLUNA = 14.0

class _Colunas:
    """Bordas das colunas, lidas do cabeçalho da tabela."""

    __slots__ = ("x_data", "x_descricao", "credito", "debito", "saldo")

    def __init__(self, x_data: float, x_descricao: float,
                 credito: float, debito: float, saldo: float) -> None:
        self.x_data = x_data
        self.x_descricao = x_descricao
        self.credito = credito
        self.debito = debito
        self.saldo = saldo

    def qual(self, x1: float) -> str | None:
        """Coluna a que pertence um valor, pela borda direita."""
        candidatos = (
            ("credito", self.credito),
            ("debito", self.debito),
            ("saldo", self.saldo),
        )
        melhor, distancia = None, _TOLERANCIA_COLUNA
        for nome, borda in candidatos:
            d = abs(x1 - borda)
            if d < distancia:
                melhor, distancia = nome, d
        return melhor


def _ler_cabecalho(linhas: list[list[dict]]) -> _Colunas | None:
    """Acha a linha `data descrição entradas R$ saídas R$ saldo R$`.

    A borda de cada coluna numérica é a do `R$` que a acompanha; quando o `R$`
    não vem, cai na borda do próprio rótulo.
    """
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if "data" not in textos or not any(t.startswith("descri") for t in textos):
            continue
        if not any(t.startswith("entrada") for t in textos):
            continue

        credito = _borda_direita(textos, palavras, "entrada")
        debito = _borda_direita(textos, palavras, "sa", excluindo=("saldo",))
        saldo = _borda_direita(textos, palavras, "saldo")
        if credito is None or debito is None or saldo is None:
            continue
        x_data = float(palavras[textos.index("data")]["x0"])
        x_desc = float(
            palavras[next(i for i, t in enumerate(textos) if t.startswith("descri"))]["x0"]
        )
        return _Colunas(x_data, x_desc, credito, debito, saldo)
    return None


# A assinatura é o CABEÇALHO DA TABELA, não a marca do banco. Procurar "itaú"
# no texto reconhecia como Itaú qualquer extrato que apenas citasse o banco —
# um PIX recebido de conta Itaú basta —, e o extrato do Nubank caía aqui. O
# cabeçalho, além de não errar, é exatamente o que cada variante sabe ler: se
# ele não está lá, o adaptador não teria o que fazer com o arquivo.
_ASSINATURA_MENSAL = re.compile(
    r"\bdata\b.*\bdescri\w*\b.*\bentradas?\b.*\bsa[íi]das?\b", re.IGNORECASE
)
# "Data Lançamentos … Valor (R$) Saldo (R$)" sozinho NÃO identifica o Itaú:
# Stone ("DATA TIPO LANÇAMENTO VALOR (R$) SALDO (R$) CONTRAPARTE") e Grafeno
# ("DATA / HORA LANÇAMENTO NOME … VALOR (R$) SALDO (R$)") têm cabeçalho quase
# igual e caíam aqui. O par "Razão Social" + "CNPJ/CPF" é o que separa — e é
# justamente a coluna que esta variante sabe ler.
_ASSINATURA_EXTRATO = re.compile(
    r"\bdata\b.*\blan[çc]amentos?\b.*\braz[ãa]o\s+social\b.*\bcnpj/cpf\b",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(
        _ASSINATURA_MENSAL.search(linha) or _ASSINATURA_EXTRATO.search(linha)
        for linha in linhas
    )


def _extrair_mensal(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    """Extrato mensal impresso — duas colunas, entradas e saídas separadas."""
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    saldo_final: Decimal | None = None
    fim_da_tabela = False
    idx = 0
    ultima_data: date | None = None

    for palavras in paginas:
        if fim_da_tabela:
            break
        linhas = _agrupar_linhas(palavras)
        colunas = _ler_cabecalho(linhas)
        if colunas is None:
            # Página sem tabela de movimentação (capa, investimentos, legendas).
            continue

        for linha in linhas:
            if fim_da_tabela:
                # Depois do fecho vem outra tabela: o "totalizador de aplicações
                # automáticas" reusa as mesmas colunas, e sua linha "na conta
                # corrente (1)" entrava como um crédito de R$ 19.070,30 que não
                # existe — bem na cauda, onde nenhuma âncora a pegaria.
                break

            # Fora a coluna da legenda, que fica à esquerda da coluna `data`.
            uteis = [p for p in linha if float(p["x0"]) >= colunas.x_data - 3]
            if not uteis:
                continue

            data_nova: date | None = None
            descricao: list[str] = []
            valores: dict[str, Decimal] = {}

            for palavra in uteis:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA_CURTA.match(texto) and abs(x0 - colunas.x_data) <= 6:
                    lida = parse_data(texto, referencia_ano)
                    if lida:
                        data_nova = lida
                    continue

                if _VALOR.match(texto):
                    coluna = colunas.qual(x1)
                    if coluna:
                        valor = parse_valor(texto)
                        if valor is not None:
                            valores[coluna] = valor
                        continue

                if x0 >= colunas.x_descricao - 3:
                    descricao.append(texto)

            if data_nova:
                ultima_data = data_nova

            texto_descricao = " ".join(descricao).strip()
            if not texto_descricao:
                continue

            # Abertura do extrato: o único saldo que vira âncora inicial.
            if re.match(r"^saldo\s+anterior$", texto_descricao, re.IGNORECASE):
                if "saldo" in valores and saldo_anterior is None:
                    saldo_anterior = valores["saldo"]
                continue

            # "Saldo em C/C" fecha a cadeia da CONTA CORRENTE, que é a que a
            # coluna de saldo acompanha. O "Saldo final" logo abaixo não serve:
            # ele soma a aplicação automática (1,00 de C/C + 3.322,86 aplicados
            # = 3.323,86) e compará-lo com a cadeia acusaria uma diferença de
            # R$ 3.322,86 que é só a aplicação, não lançamento perdido.
            if re.match(r"^saldo\s+em\s+c/?c$", texto_descricao, re.IGNORECASE):
                if "saldo" in valores:
                    saldo_final = valores["saldo"]
                continue

            # "Saldo final" encerra a movimentação; o que vem depois é outra
            # tabela. Ele não ancora nada — ver o comentário acima.
            if re.match(r"^saldo\s+final$", texto_descricao, re.IGNORECASE):
                fim_da_tabela = True
                break

            # Saldo de aplicação automática impresso na coluna de saldo da conta
            # corrente: não é lançamento e não pode ancorar a cadeia.
            if texto_descricao.upper().startswith("SALDO"):
                continue

            if "credito" in valores:
                valor = abs(valores["credito"])
            elif "debito" in valores:
                valor = -abs(valores["debito"])
            else:
                continue

            if valor == 0 or ultima_data is None:
                continue

            historico = re.sub(r"\s+", " ", texto_descricao)[:200]
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(ultima_data, historico, valor, idx),
                    data=ultima_data,
                    valor=valor,
                    historico=historico,
                    tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
                    saldo_apos=valores.get("saldo"),
                    ordem=idx,
                )
            )
            idx += 1

    if not transacoes:
        return []
    return [
        Bloco(
            transacoes=transacoes,
            saldo_anterior=saldo_anterior,
            saldo_final=saldo_final,
        )
    ]


# ──────────────────────────────────────────── variante: extrato do internet banking

# Cabeçalho: `Data Lançamentos Razão Social CNPJ/CPF Valor (R$) Saldo (R$)`
_DATA_LONGA = re.compile(r"^\d{2}/\d{2}/\d{4}$")


class _ColunasExtrato:
    """Bordas da variante de coluna única (extrato do internet banking)."""

    __slots__ = ("x_data", "x_lancamento", "valor", "saldo")

    def __init__(self, x_data: float, x_lancamento: float,
                 valor: float, saldo: float) -> None:
        self.x_data = x_data
        self.x_lancamento = x_lancamento
        self.valor = valor
        self.saldo = saldo


def _ler_cabecalho_extrato(linhas: list[list[dict]]) -> _ColunasExtrato | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if "data" not in textos or not any(t.startswith("lan") for t in textos):
            continue
        if "valor" not in textos or "saldo" not in textos:
            continue

        valor = _borda_direita(textos, palavras, "valor")
        saldo = _borda_direita(textos, palavras, "saldo")
        if valor is None or saldo is None:
            continue

        return _ColunasExtrato(
            x_data=float(palavras[textos.index("data")]["x0"]),
            x_lancamento=float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("lan"))]["x0"]
            ),
            valor=valor,
            saldo=saldo,
        )
    return None


def _extrair_internet(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    """Extrato do internet banking — uma coluna de valor, saldo só no fim do dia.

    Difere do extrato mensal em tudo o que importa:

    - **Uma coluna de valor**, com o sinal no número (`-2.225,00`), em vez das
      colunas separadas de entradas e saídas.
    - **O saldo aparece uma vez por dia**, na linha `SALDO TOTAL DISPONÍVEL DIA`,
      que não é lançamento. Ela vira a âncora do dia: o saldo dela é copiado para
      o último lançamento anterior, e a conferência por segmentos do validador
      passa a fechar dia a dia.
    - **A razão social quebra em várias linhas** em torno da linha de dados — a
      de cima e a de baixo são a mesma razão social ("CARGO TIME EXPRESS" /
      "TRANSPORTES LTDA"). Cada fragmento é colado no lançamento verticalmente
      mais próximo: a quebra fica a ~5 pontos da linha de dados e o lançamento
      seguinte a ~13, então não há empate.
    """
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0

    for palavras in paginas:
        linhas = _agrupar_linhas(palavras)
        colunas = _ler_cabecalho_extrato(linhas)
        if colunas is None:
            continue

        # (topo, posição) das linhas de dados desta página, para colar os
        # fragmentos de razão social no lançamento mais próximo.
        ancoras: list[tuple[float, int]] = []
        fragmentos: list[tuple[float, str]] = []

        # Nada acima do cabeçalho da tabela é fragmento. Sem este corte, o
        # cabeçalho da página (razão social, CNPJ e agência do TITULAR) é o
        # texto solto mais próximo do primeiro lançamento e entra no histórico
        # dele — o próprio pagador virava a contraparte da primeira linha.
        topo_cabecalho = min(
            (
                float(linha[0]["top"])
                for linha in linhas
                if _ler_cabecalho_extrato([linha]) is not None
            ),
            default=0.0,
        )

        for linha in linhas:
            topo = float(linha[0]["top"])
            if topo <= topo_cabecalho:
                continue
            data_lida: date | None = None
            descricao: list[str] = []
            valor: Decimal | None = None
            saldo: Decimal | None = None

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA_LONGA.match(texto) and abs(x0 - colunas.x_data) <= 6:
                    data_lida = parse_data(texto, referencia_ano)
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

            if data_lida is None and valor is None and texto_descricao:
                # Fragmento de razão social quebrada.
                fragmentos.append((topo, texto_descricao))
                continue

            if re.match(r"^saldo\s+anterior$", texto_descricao, re.IGNORECASE):
                if saldo is not None and saldo_anterior is None:
                    saldo_anterior = saldo
                continue

            if texto_descricao.upper().startswith("SALDO"):
                # Fecho do dia: ancora a cadeia no último lançamento lido.
                if saldo is not None and transacoes:
                    transacoes[-1] = replace(transacoes[-1], saldo_apos=saldo)
                continue

            if valor is None or valor == 0 or data_lida is None:
                continue

            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(data_lida, texto_descricao, valor, idx),
                    data=data_lida,
                    valor=valor,
                    historico=texto_descricao[:200],
                    tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
                    saldo_apos=None,
                    ordem=idx,
                )
            )
            ancoras.append((topo, len(transacoes) - 1))
            idx += 1

        transacoes = colar_fragmentos(transacoes, ancoras, fragmentos)

    if not transacoes:
        return []
    # O rótulo de ausência só vale depois da colagem: há lançamento cuja
    # descrição inteira está nos fragmentos em volta (o rendimento de aplicação
    # automática é um), e aplicá-lo antes deixava "SEM DESCRIÇÃO" grudado na
    # frente do histórico correto.
    transacoes = [
        t if t.historico else replace(t, historico="SEM DESCRIÇÃO")
        for t in transacoes
    ]
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    """Escolhe a variante de layout pelo cabeçalho da tabela.

    O Itaú emite dois extratos com a mesma marca e nenhuma semelhança de
    layout: o **mensal impresso** (duas colunas na página, entradas e saídas
    separadas) e o **do internet banking** (coluna única, saldo por dia). Um
    banco, dois formatos — e é a razão de o despacho por `banco_sigla` levar a
    um módulo, e não direto a um parser.
    """
    for palavras in paginas:
        linhas = _agrupar_linhas(palavras)
        if _ler_cabecalho(linhas) is not None:
            return _extrair_mensal(paginas, referencia_ano)
        if _ler_cabecalho_extrato(linhas) is not None:
            return _extrair_internet(paginas, referencia_ano)
    return []
