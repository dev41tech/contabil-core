"""Extrato do Sicoob — sinal como letra colada no valor e detalhe em linhas soltas.

O layout é:

    PERÍODO: 01/07/2025 - 31/07/2025
    DATA  HISTÓRICO              VALOR
    31/07 SALDO DO DIA           11.194,89D
    31/07 TARIFA COBRANÇA             2,50D
          DOC.: 715129
    31/07 PIX EMIT.OUTRA IF         330,00D
          Pagamento Pix
          ***.641.126-**
          DOC.: Pix

O que ele tem de próprio:

- **O sinal é uma letra colada no número** — `330,00D` é débito, `330,00C` é
  crédito. Sem tratar isso, `parse_valor` não converte e a linha inteira se
  perde.
- **`SALDO DO DIA` é o fechamento do dia e vem impresso no topo dele**, com a
  lista em ordem decrescente — mesma armadilha do BBC e da Cresol: o saldo
  pertence ao ÚLTIMO lançamento do dia em ordem cronológica.
- **O detalhe vem em linhas soltas abaixo** (`DOC.:`, nome do pagador, CPF
  mascarado). Elas pertencem ao lançamento de cima e valem coladas: é ali que
  está o documento da contraparte.
- **A data não tem ano** (`31/07`); ele sai do `PERÍODO:` do cabeçalho.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"SICOOB", "756"})

# 11.194,89D  /  2,50C
_VALOR_DC = r"\d{1,3}(?:\.\d{3})*,\d{2}[DC]"

_LINHA = re.compile(rf"^(\d{{2}}/\d{{2}})\s+(.*?)\s+({_VALOR_DC})\s*$")
_SALDO_DIA = re.compile(rf"^(\d{{2}}/\d{{2}})\s+SALDO DO DIA\s+({_VALOR_DC})\s*$", re.I)
_PERIODO = re.compile(r"PER[ÍI]ODO:\s*\d{2}/\d{2}/(\d{4})", re.IGNORECASE)

_ASSINATURA = re.compile(
    r"^\s*DATA\s+HIST[ÓO]RICO\s+VALOR\s*$", re.IGNORECASE | re.MULTILINE
)

_IGNORAR = re.compile(
    r"^(SICOOB|SISTEMA DE COOPERATIVAS|PLATAFORMA DE|COOP\.|CONTA:|PER[ÍI]ODO:|"
    r"HIST[ÓO]RICO DE MOVIMENTA|DATA\s+HIST)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.match(linha) for linha in linhas)


def _valor_com_letra(texto: str) -> Decimal | None:
    """`330,00D` → −330,00; `330,00C` → +330,00."""
    letra = texto[-1].upper()
    numero = parse_valor(texto[:-1])
    if numero is None:
        return None
    return -abs(numero) if letra == "D" else abs(numero)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    ano = referencia_ano
    for linha in linhas:
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

    dias: list[tuple[date, Decimal | None, list[TransacaoOFX]]] = []
    idx = 0

    for bruta in linhas:
        linha = bruta.strip()
        if not linha or _IGNORAR.match(linha):
            continue

        fechamento = _SALDO_DIA.match(linha)
        if fechamento:
            do_dia = data_de(fechamento.group(1))
            if do_dia is None:
                continue
            # Dia partido por quebra de página repete o cabeçalho.
            if dias and dias[-1][0] == do_dia:
                continue
            dias.append((do_dia, _valor_com_letra(fechamento.group(2)), []))
            continue

        casada = _LINHA.match(linha)
        if casada:
            data_str, descricao, valor_str = casada.groups()
            data_lida = data_de(data_str)
            valor = _valor_com_letra(valor_str)
            if data_lida is None or valor is None or valor == 0:
                continue
            if not dias:
                dias.append((data_lida, None, []))
            historico = re.sub(r"\s+", " ", descricao).strip() or "SEM DESCRIÇÃO"
            dias[-1][2].append(
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
            idx += 1
            continue

        # Linha de detalhe: pertence ao lançamento imediatamente acima.
        if dias and dias[-1][2]:
            alvo = dias[-1][2][-1]
            juntado = f"{alvo.historico} {linha}".strip()[:200]
            dias[-1][2][-1] = replace(alvo, historico=juntado)

    transacoes: list[TransacaoOFX] = []
    for _do_dia, saldo, lancamentos in reversed(dias):
        if not lancamentos:
            continue
        cronologicos = list(reversed(lancamentos))
        if saldo is not None:
            cronologicos[-1] = replace(cronologicos[-1], saldo_apos=saldo)
        transacoes.extend(cronologicos)

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes)]
