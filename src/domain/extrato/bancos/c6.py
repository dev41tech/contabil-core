"""Extrato do C6 — duas colunas de data e a descrição quebrada em volta da linha.

O layout é:

    Data       Data
    lançamento contábil  Tipo          Descrição                        Valor
    02/05      02/05     Entrada PIX   Pix recebido de Fulano       R$ 7.332,80
    02/05      02/05     Pagamento     EXEMPLO COMERCIO - ME       -R$ 1.839,00
    Saldo do dia 02/05/25                                              R$ 3,91
    Pix recebido de EXEMPLO EQUIPAMENTOS HIDRAULICOS LTDA EM RECUPERACAO
    06/05      06/05     Entrada PIX                                R$ 20.000,00
    JUDICIAL

O que ele tem de próprio:

- **Duas datas por lançamento** — a do lançamento e a contábil. Vale a primeira,
  que é a que o extrato ordena e a que o contador concilia.
- **Nenhuma das duas tem ano** (`02/05`). O ano sai do cabeçalho do período
  ("Maio 2025 ( 01/05/2025 - 31/05/2025 )").
- **A descrição longa quebra acima e abaixo da linha de dados**, e no C6 isso
  acontece sem deixar a linha vazia: `06/05 06/05 Entrada PIX R$ 20.000,00` fica
  só com o tipo, e o nome da contraparte está nas duas linhas em volta. Como o
  C6 só emite linha de texto solta nesse caso — dois lançamentos seguidos nunca
  têm nada entre eles —, colar as vizinhas é seguro.
- **`Saldo do dia` vem depois dos lançamentos daquele dia** e a lista é
  crescente, então ele é o fechamento e ancora o último lançamento do dia.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"C6", "C6BANK", "336"})

_MOEDA = r"-?R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}"

_LINHA = re.compile(rf"^(\d{{2}}/\d{{2}})\s+(\d{{2}}/\d{{2}})\s+(.*?)\s+({_MOEDA})\s*$")
_SALDO_DIA = re.compile(
    rf"^Saldo do dia\s+(\d{{2}}/\d{{2}}/\d{{2}})\s+({_MOEDA})\s*$", re.IGNORECASE
)
_PERIODO = re.compile(r"\(\s*\d{2}/\d{2}/(\d{4})\s*-")

_ASSINATURA = re.compile(r"^Saldo do dia\s+\d{2}/\d{2}/\d{2}\s+R\$", re.IGNORECASE)

_IGNORAR = re.compile(
    r"^(Extrato\s|Ag[eê]ncia:|Saldo do dia\s|Data\s*$|Tipo\s|lan[çc]amento\s|"
    r"\w+\s+\d{4}\s*\()",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.match(linha.strip()) for linha in linhas)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    limpas = [ln.strip() for ln in linhas]

    ano = referencia_ano
    for linha in limpas:
        achado = _PERIODO.search(linha)
        if achado:
            ano = int(achado.group(1))
            break

    def data_de(texto: str) -> date | None:
        dia, mes = texto.split("/")
        try:
            return date(ano, int(mes), int(dia))
        except ValueError:
            return None

    def eh_estrutural(i: int) -> bool:
        if not (0 <= i < len(limpas)) or not limpas[i]:
            return True
        return bool(
            _LINHA.match(limpas[i])
            or _SALDO_DIA.match(limpas[i])
            or _IGNORAR.match(limpas[i])
        )

    transacoes: list[TransacaoOFX] = []
    idx = 0
    pendentes_do_dia: list[int] = []

    for i, linha in enumerate(limpas):
        if not linha:
            continue

        fechamento = _SALDO_DIA.match(linha)
        if fechamento:
            saldo = parse_valor(fechamento.group(2))
            if saldo is not None and pendentes_do_dia:
                ultimo = pendentes_do_dia[-1]
                transacoes[ultimo] = replace(transacoes[ultimo], saldo_apos=saldo)
            pendentes_do_dia = []
            continue

        casada = _LINHA.match(linha)
        if not casada:
            continue

        data_str, _contabil, meio, valor_str = casada.groups()
        data_lida = data_de(data_str)
        valor = parse_valor(valor_str)
        if data_lida is None or valor is None or valor == 0:
            continue

        acima = limpas[i - 1] if i > 0 and not eh_estrutural(i - 1) else ""
        abaixo = limpas[i + 1] if not eh_estrutural(i + 1) else ""
        historico = " ".join(p for p in (acima, meio.strip(), abaixo) if p)
        historico = re.sub(r"\s+", " ", historico).strip() or "SEM DESCRIÇÃO"

        transacoes.append(
            TransacaoOFX(
                fitid=gerar_fitid(data_lida, historico, valor, idx),
                data=data_lida,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=None,
                ordem=idx,
            )
        )
        pendentes_do_dia.append(len(transacoes) - 1)
        idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes)]



