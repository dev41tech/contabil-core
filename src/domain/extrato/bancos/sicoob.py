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
# O `R$` entra no grupo do VALOR, e não na descrição, porque o Internet
# Banking novo do Sicoob (SISBR) imprime `R$ 600,00D` onde o layout antigo
# imprimia `600,00D`. Fora do grupo, o cifrão era engolido pelo `.*?` da
# descrição — e a linha de `SALDO DO DIA`, que casa o valor logo depois do
# rótulo, deixava de casar: o extrato perdia TODAS as âncoras de saldo.
_VALOR_DC = r"(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}[DC]"

_LINHA = re.compile(rf"^(\d{{2}}/\d{{2}})\s+(.*?)\s+({_VALOR_DC})\s*$")
_SALDO_DIA = re.compile(rf"^(\d{{2}}/\d{{2}})\s+SALDO DO DIA\s+({_VALOR_DC})\s*$", re.I)
_SALDO_ANTERIOR = re.compile(
    rf"^(\d{{2}}/\d{{2}})\s+SALDO ANTERIOR\s+({_VALOR_DC})\s*$", re.I
)
# Captura as DUAS pontas: a de fim decide o ano de uma data que cai fora do
# período. `31/12` num extrato de janeiro/2026 é de 2025, e sem isso o saldo
# anterior era datado quase um ano no futuro.
_PERIODO = re.compile(
    r"PER[ÍI]ODO:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)

# `Documento` é coluna do export novo e não existe no antigo — daí o opcional.
_ASSINATURA = re.compile(
    r"^\s*DATA\s+(?:DOCUMENTO\s+)?HIST[ÓO]RICO\s+VALOR\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_IGNORAR = re.compile(
    r"^(SICOOB|SISTEMA DE COOPERATIVAS|PLATAFORMA DE|COOP\.|CONTA:|PER[ÍI]ODO:|"
    r"COOPERATIVA:|EXTRATO DE CONTA|SALDO BLOQUEADO|SALDO EM CONTA|RESUMO|"
    r"HIST[ÓO]RICO DE MOVIMENTA|DATA\s+(?:DOCUMENTO\s+)?HIST)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.match(linha) for linha in linhas)


def _valor_com_letra(texto: str) -> Decimal | None:
    """`330,00D` → −330,00; `330,00C` → +330,00. Aceita `R$ 330,00D`."""
    texto = re.sub(r"^R\$\s*", "", texto.strip())
    letra = texto[-1].upper()
    numero = parse_valor(texto[:-1])
    if numero is None:
        return None
    return -abs(numero) if letra == "D" else abs(numero)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    ano = referencia_ano
    fim_periodo: date | None = None
    for linha in linhas:
        achado = _PERIODO.search(linha)
        if achado:
            d, m, a = achado.group(2).split("/")
            fim_periodo = date(int(a), int(m), int(d))
            ano = fim_periodo.year
            break

    def data_de(texto: str) -> date | None:
        dia, mes = texto.split("/")
        try:
            lida = date(ano, int(mes), int(dia))
        except ValueError:
            return None
        # Data depois do fim do período só pode ser do ano anterior: é o
        # `31/12` que abre um extrato de janeiro.
        if fim_periodo is not None and lida > fim_periodo:
            try:
                return lida.replace(year=ano - 1)
            except ValueError:
                return None
        return lida

    # A LINHA DE SALDO TRAZ A PRÓPRIA DATA, e é isso que decide a qual dia ela
    # pertence — não a posição dela no arquivo.
    #
    # O layout antigo imprime `SALDO DO DIA` no TOPO do bloco do dia; o Internet
    # Banking novo (SISBR) imprime no FIM. Amarrado à posição, o adaptador jogava
    # os lançamentos de 02/01 no balde do dia 05/01 e a cadeia acusava um buraco
    # do tamanho de um dia inteiro. Agrupar por data lê os dois layouts sem
    # precisar distingui-los.
    lancamentos: list[TransacaoOFX] = []
    saldos: dict[date, Decimal] = {}
    saldo_anterior: Decimal | None = None
    idx = 0

    for bruta in linhas:
        linha = bruta.strip()
        if not linha or _IGNORAR.match(linha):
            continue

        # ANTES de `_LINHA`, que também casaria — e casava: a abertura entrava
        # como um lançamento de R$ 7.879,98. A cadeia de saldos NÃO pegava isso,
        # porque o valor falso é exatamente o saldo de abertura e está na
        # primeira posição, então a soma fechava. Quem pegou foi o OFX do mesmo
        # período, que trazia um lançamento a menos.
        abertura = _SALDO_ANTERIOR.match(linha)
        if abertura:
            if saldo_anterior is None:
                saldo_anterior = _valor_com_letra(abertura.group(2))
            continue

        fechamento = _SALDO_DIA.match(linha)
        if fechamento:
            do_dia = data_de(fechamento.group(1))
            saldo = _valor_com_letra(fechamento.group(2))
            if do_dia is not None and saldo is not None:
                # Dia partido por quebra de página repete a linha; o primeiro
                # valor lido é o do dia e os repetidos são idênticos.
                saldos.setdefault(do_dia, saldo)
            continue

        casada = _LINHA.match(linha)
        if casada:
            data_str, descricao, valor_str = casada.groups()
            data_lida = data_de(data_str)
            valor = _valor_com_letra(valor_str)
            if data_lida is None or valor is None or valor == 0:
                continue
            historico = re.sub(r"\s+", " ", descricao).strip() or "SEM DESCRIÇÃO"
            lancamentos.append(
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
        if lancamentos:
            alvo = lancamentos[-1]
            juntado = f"{alvo.historico} {linha}".strip()[:200]
            lancamentos[-1] = replace(alvo, historico=juntado)

    # O extrato sai do mais recente para o mais antigo, e dentro do dia também.
    transacoes = list(reversed(lancamentos))

    # O saldo do dia é o que vale DEPOIS do último lançamento dele — e "último"
    # só existe depois de ordenar.
    ultimo_do_dia: dict[date, int] = {}
    for posicao, transacao in enumerate(transacoes):
        ultimo_do_dia[transacao.data] = posicao
    for do_dia, saldo in saldos.items():
        posicao = ultimo_do_dia.get(do_dia)
        if posicao is not None:
            transacoes[posicao] = replace(transacoes[posicao], saldo_apos=saldo)

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]
