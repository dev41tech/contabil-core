"""Extrato do Bradesco (X-One) — lançamento em três linhas.

O layout é:

    PIX ENVIADO                       ← tipo do lançamento (linha de texto puro)
    1406542 -2.000,00 36.771,60       ← linha de dados: [data] doc valor saldo
    DES: GRISOPAR COM TRANSPOR 02/01  ← complemento: a contraparte

O parser genérico lia só a linha de cima (`pending_desc`) e deixava a de baixo
solta — que então era consumida pelo lançamento SEGUINTE. Resultado: o nome da
contraparte de um PIX aparecia no histórico da tarifa debaixo dele, e a cadeia
de saldos quebrava. Era o defeito que fazia o Bradesco extrair 119 lançamentos
e nenhum importar.

A regra que separa os dois formatos é a própria linha de dados:

- **tem letras** (`RENTAB.INVEST FACILCRED* 3827085 0,01 38.771,60`) → o
  lançamento se descreve sozinho; a linha de texto acima não é dele.
- **só dígitos** (`1406542 -2.000,00 36.771,60`) → é o miolo de um grupo de
  três: o tipo está acima e o complemento abaixo.

O complemento também aparece colado na linha de dados quando o grupo cai numa
quebra de página (`DES: Luan A. Ribeiro de 09/01 1220597 -2.275,57 7.679,41`).
Esse caso cai no primeiro ramo e o histórico já sai com a contraparte, que é a
parte que interessa para a conciliação.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"BRADESCO", "237"})

_VALOR = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"

# [data] <resto> <valor> <saldo>
_LINHA_DADOS = re.compile(
    rf"^(?:(\d{{2}}/\d{{2}}/\d{{4}})\s+)?(.*?)\s+({_VALOR})\s+({_VALOR})\s*$"
)

_TEM_LETRA = re.compile(r"[A-Za-zÀ-ÿ]")

# Cabeçalho de colunas: abre um bloco novo. O arquivo pode trazer "Extrato"
# (o mês fechado) e "Últimos Lançamentos" (os dias seguintes), cada um com sua
# própria cadeia de saldos.
_CABECALHO_TABELA = re.compile(r"^Data\s+Lan[cç]amento\s+Dcto", re.IGNORECASE)
_SALDO_ANTERIOR = re.compile(
    rf"SALDO ANTERIOR\s+({_VALOR})\s*$", re.IGNORECASE
)

_IGNORAR = re.compile(
    r"^(Ag[eê]ncia\s*\||Extrato de:|Data\s+Lan[cç]amento|Total\s|"
    r"SALDO ANTERIOR|Os dados|Pr[oó]ximo|\d{5}\s*\|)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    """Assinatura do extrato: o cabeçalho de colunas do X-One."""
    for linha in linhas[:40]:
        if re.search(r"Data\s+Lan[cç]amento\s+Dcto", linha, re.IGNORECASE):
            return True
        if "bradesco" in linha.lower():
            return True
    return False


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    """Devolve um bloco por trecho de extrato com cadeia de saldos própria."""
    limpas = [ln.strip() for ln in linhas]
    blocos: list[Bloco] = []
    atuais: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0
    ultima_data: date | None = None

    def fechar() -> None:
        nonlocal atuais, saldo_anterior
        if atuais:
            blocos.append(Bloco(transacoes=atuais, saldo_anterior=saldo_anterior))
        atuais = []
        saldo_anterior = None

    def eh_dados(i: int) -> bool:
        return (
            0 <= i < len(limpas)
            and bool(limpas[i])
            and not _IGNORAR.match(limpas[i])
            and _LINHA_DADOS.match(limpas[i]) is not None
        )

    for i, linha in enumerate(limpas):
        if not linha:
            continue

        if _CABECALHO_TABELA.match(linha):
            fechar()
            ultima_data = None
            continue

        m_saldo = _SALDO_ANTERIOR.search(linha)
        if m_saldo:
            # Abre a cadeia do bloco: dá contra o que conferir o 1º lançamento.
            saldo_anterior = parse_valor(m_saldo.group(1))
            d = parse_data(linha, referencia_ano)
            if d:
                ultima_data = d
            continue

        if _IGNORAR.match(linha):
            continue

        m = _LINHA_DADOS.match(linha)
        if not m:
            continue

        data_str, meio, valor_str, saldo_str = m.groups()
        if data_str:
            d = parse_data(data_str, referencia_ano)
            if d:
                ultima_data = d
        if ultima_data is None:
            continue

        valor = parse_valor(valor_str)
        if valor is None or valor == 0:
            continue

        meio = meio.strip()
        if _TEM_LETRA.search(meio):
            historico = meio
        else:
            tipo = ""
            if (
                i > 0
                and not eh_dados(i - 1)
                and _TEM_LETRA.search(limpas[i - 1] or "")
                and not _IGNORAR.match(limpas[i - 1])
                and not _CABECALHO_TABELA.match(limpas[i - 1])
            ):
                tipo = limpas[i - 1]
            complemento = ""
            if i + 1 < len(limpas) and not eh_dados(i + 1):
                seguinte = limpas[i + 1]
                if seguinte and _TEM_LETRA.search(seguinte) and not _IGNORAR.match(seguinte):
                    complemento = seguinte
            historico = " ".join(p for p in (tipo, complemento) if p).strip()

        historico = re.sub(r"\s+", " ", historico) or "SEM DESCRIÇÃO"

        atuais.append(
            TransacaoOFX(
                fitid=gerar_fitid(ultima_data, historico, valor, idx),
                data=ultima_data,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
                saldo_apos=parse_valor(saldo_str),
                ordem=idx,
            )
        )
        idx += 1

    fechar()
    return blocos
