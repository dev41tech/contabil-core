"""Cruzamento razão × extrato, em camadas, sem banco de dados.

Medido na conta 2699 da BLD (fev/2025): 3.277 de 3.280 lançamentos casam no
mesmo dia pelo valor exato. O trabalho do contador está nas sobras, e o que este
módulo precisa fazer bem é classificá-las.

O VALOR SOZINHO ESCOLHE O PAR ERRADO

No mesmo relatório, o banco tem UMA entrada de 520.000,00 em 24/02 ("Sispag X
ONE EXPRESS LT") e o razão tem DUAS: "VALOR REF X ONE EXPRESS LTDA" e
"TRANSFERENCIA ENTRE CONTAS". Pelo valor, qualquer uma serve; casando a
primeira que aparece, o relatório acusava a lançamento certo como sobra. Dentro
do mesmo dia e valor, o par é decidido pela semelhança do histórico.

AS CAMADAS, NA ORDEM

1. mesmo dia, mesmo valor                      → CONCILIADO
2. mesmo valor, até N dias de distância         → DATA_DIFERENTE
3. vários de um lado somam um do outro, no dia  → AGRUPADO
3a. lote do SISPAG (se a consulta foi enviada)  → LOTE_SISPAG / LOTE_SISPAG_DIVERGENTE
3b. a sobra inteira do dia fecha com o extrato  → LOTE_DO_DIA
4. sobra que repete dia e valor de um par feito → DUPLICIDADE_RAZAO / DUPLICIDADE_EXTRATO
5. mesmo dia, valor muito próximo               → VALOR_DIVERGENTE
6. o resto                                      → SO_RAZAO / SO_EXTRATO

A ordem importa: duplicidade só é afirmada depois que a janela de dias e os
agrupamentos tiveram a chance de achar par para o lançamento repetido.

LOTES DE SISPAG — O QUE A SOMA SOZINHA NÃO RESOLVE

O Itaú paga TED e crédito em conta em lote: UMA linha "Sispag Fornecedores" no
extrato, dezenas de lançamentos no razão. A camada 3 não alcança (até 6 itens),
e procurar "qual combinação de lançamentos soma a linha" é ambíguo de verdade:
em 22/04/2025 da BLD, 13 dos 54 lançamentos que sobraram podiam entrar ou não na
linha de 157.854,54, e todas as escolhas fechavam. Escolher uma é acertar a soma
e errar a composição. Por isso nenhuma camada daqui escolhe entre combinações.

- 3a usa a consulta de pagamentos do SISPAG: o banco junta por DIA e TIPO, e os
  pagamentos de um tipo que somam uma linha (ou duas, quando o lote saiu
  partido) SÃO a composição dela. Cada pagamento casa com o razão pelo valor, e
  o nome do favorecido desempata. Pagamento que não está no razão vira
  pendência com o nome dele — é a diferença apontada, não só medida.
- 3b não precisa de arquivo: quando TUDO o que sobrou num dia, num sentido,
  soma exatamente o que sobrou do extrato naquele dia, não há o que escolher.
  Em abr/2025 isso fechou 4 dos 6 dias com pendência.

APLICAÇÃO AUTOMÁTICA QUE O EXTRATO NÃO TRAZ

A conta banco movimenta a conta de aplicação (na BLD, 2699 ↔ 2700): resgate,
aplicação e rendimento pago. O extrato mensal e o consolidado imprimem esses
lançamentos, e eles casam dia a dia, no centavo (conferido em mar, ago e dez de
2025). O extrato do internet banking não: traz os resgates e nunca as
aplicações, e às vezes nem os resgates. Sem cuidado, cada aplicação do razão
virava "só no razão" — pendência falsa, porque a informação vem de outro
documento (o relatório de aplicações do banco).

A regra é por ESPÉCIE (resgate, aplicação, rendimento): se o extrato do período
não tem nenhum lançamento daquela espécie, os do razão saem das pendências e
vão para `aplicacao_sem_extrato`, que o relatório mostra com aviso. Se o extrato
tem ao menos um, a espécie é conferida como qualquer outro lançamento — e o que
sobrar é pendência de verdade. Eles saem ANTES das camadas: um resgate de
1.040,30 sem par não pode casar com um PIX recebido de mesmo valor dois dias
depois.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.core.texto import tokens_para_match
from src.domain.conciliacao_bancaria.sispag import PagamentoSispag

CONCILIADO = "CONCILIADO"
DATA_DIFERENTE = "DATA_DIFERENTE"
AGRUPADO = "AGRUPADO"
LOTE_SISPAG = "LOTE_SISPAG"
LOTE_SISPAG_DIVERGENTE = "LOTE_SISPAG_DIVERGENTE"
LOTE_DO_DIA = "LOTE_DO_DIA"
DUPLICIDADE_RAZAO = "DUPLICIDADE_RAZAO"
DUPLICIDADE_EXTRATO = "DUPLICIDADE_EXTRATO"
VALOR_DIVERGENTE = "VALOR_DIVERGENTE"
SO_RAZAO = "SO_RAZAO"
SO_EXTRATO = "SO_EXTRATO"

# Soma com mais itens que isso é mais coincidência que agrupamento, e o número
# de combinações explode.
_MAX_ITENS_AGRUPADOS = 6
_MAX_CANDIDATOS_AGRUPAMENTO = 20
# "Valor muito próximo": o que cabe num erro de digitação ou num centavo de
# tarifa, não dois pagamentos diferentes que por acaso têm valor parecido.
_DIVERGENCIA_MAX_ABSOLUTA = Decimal("50.00")
_DIVERGENCIA_MAX_RELATIVA = Decimal("0.01")
# Um lote do SISPAG sai em até tantas linhas do extrato no mesmo dia. Em
# abr/2025 o máximo visto foi 2 (17/04, crédito em conta partido em dois).
_MAX_LINHAS_POR_LOTE = 3

# Abertura do período: o saldo do mês anterior lançado como movimento. No
# banco isso é "saldo anterior", não transação — nunca vai ter par.
_ABERTURA = re.compile(r"SALDO\s+(NEGATIVO|ANTERIOR|INICIAL)", re.IGNORECASE)

# "APLIC" como começo de palavra: RESGATE DE APLICAÇÃO AUTOMÁTICA, APL APLIC AUT
# MAIS, REND PAGO APLIC AUT MAIS. Sem a exigência da palavra, "REND" achava
# "PGTO JAIME CARLOS MARENDA" — medido no razão da 2699.
_APLICACAO = re.compile(r"\bAPLIC", re.IGNORECASE)
_RENDIMENTO = re.compile(r"\bREND", re.IGNORECASE)
RENDIMENTO = "RENDIMENTO"
RESGATE = "RESGATE"
APLICACAO = "APLICACAO"


def especie_de_aplicacao(historico: str, valor: Decimal) -> str | None:
    """Rendimento, resgate ou aplicação automática — ou `None` se não é.

    Visto da conta banco: entrada é resgate, saída é aplicação.
    """
    if not _APLICACAO.search(historico):
        return None
    if _RENDIMENTO.search(historico):
        return RENDIMENTO
    return RESGATE if valor > 0 else APLICACAO


@dataclass(frozen=True)
class Item:
    id: str
    data: date
    valor: Decimal
    historico: str


@dataclass
class Grupo:
    tipo: str
    razao: list[Item] = field(default_factory=list)
    extrato: list[Item] = field(default_factory=list)
    # Lote do SISPAG com pagamento que não achou par no razão: pago pelo banco,
    # não lançado (ou lançado com outro valor/data).
    sispag_faltando: list[PagamentoSispag] = field(default_factory=list)

    @property
    def diferenca(self) -> Decimal:
        return sum((i.valor for i in self.razao), Decimal("0")) - sum(
            (i.valor for i in self.extrato), Decimal("0")
        )


@dataclass
class Resultado:
    conciliados: list[Grupo]
    pendencias: list[Grupo]
    abertura: list[Item]
    # Lançamentos de aplicação automática do razão de uma espécie que o extrato
    # não traz: não conferidos, e por isso nem conciliados nem pendência.
    aplicacao_sem_extrato: list[Item] = field(default_factory=list)


def _semelhanca(a: str, b: str) -> float:
    ta, tb = set(tokens_para_match(a)), set(tokens_para_match(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _parear_por_semelhanca(razao: list[Item], extrato: list[Item]) -> list[tuple[Item, Item]]:
    """Pares mais parecidos primeiro — guloso, suficiente para grupos pequenos."""
    candidatos = sorted(
        ((_semelhanca(r.historico, e.historico), i, j) for i, r in enumerate(razao) for j, e in enumerate(extrato)),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    usados_r, usados_e, pares = set(), set(), []
    for _, i, j in candidatos:
        if i in usados_r or j in usados_e:
            continue
        usados_r.add(i)
        usados_e.add(j)
        pares.append((razao[i], extrato[j]))
    return pares


_SISPAG = re.compile(r"sispag", re.IGNORECASE)


def _linhas_do_lote(linhas: list[Item], total: Decimal) -> list[Item] | None:
    """As linhas do extrato cuja soma é o lote — só se a resposta for uma.

    Duas combinações com os mesmos valores (duas linhas de 129,13) são a mesma
    resposta. Com valores diferentes, é ambíguo, e o lote fica para a camada
    seguinte. Mais de uma linha só vale entre linhas de Sispag: juntar uma
    tarifa com um PIX porque a soma bateu seria coincidência, não lote.
    """
    for k in range(1, min(_MAX_LINHAS_POR_LOTE, len(linhas)) + 1):
        achadas = [
            c for c in itertools.combinations(linhas, k)
            if -sum(e.valor for e in c) == total
            and (k == 1 or all(_SISPAG.search(e.historico) for e in c))
        ]
        if not achadas:
            continue
        if len({tuple(sorted(e.valor for e in c)) for c in achadas}) > 1:
            return None
        return list(achadas[0])
    return None


# Palavras de razão social que não identificam ninguém.
_GENERICOS = frozenset({
    "ltda", "eireli", "epp", "me", "sa", "cia", "comercio", "servicos", "transportes",
    "transporte", "logistica", "industria", "empresa", "brasil", "do", "da", "de", "dos",
})


def _nomes(texto: str) -> set[str]:
    return {
        t for t in tokens_para_match(texto)
        if len(t) >= 3 and not t.isnumeric() and t not in _GENERICOS
    }


def _e_do_favorecido(historico: str, favorecido: str) -> bool:
    """O histórico nomeia o favorecido? Dois nomes em comum, ou o único que ele tem."""
    nomes = _nomes(favorecido)
    comuns = nomes & set(tokens_para_match(historico))
    return len(comuns) >= 2 or (len(nomes) == 1 and len(comuns) == 1)


def _recuperar_do_par_simples(
    pagamento: PagamentoSispag, conciliados: list[Grupo], livres_e: dict
) -> Item | None:
    """Tira do par 1 × 1 o lançamento do razão que o SISPAG diz ser deste lote.

    A camada 1 casa pelo dia e pelo valor. Em 17/04/2025 ela pôs "SISPAG
    FORNECEDORES CLAUDIO ALEXANDRE" (20.000,00) com a linha "Sispag Salários" de
    20.000,00 — mesmo dia, mesmo valor, pagamentos diferentes. O SISPAG mostra o
    Claudio no lote de crédito em conta. Só desfaz quando o histórico do razão
    nomeia o favorecido e o do extrato não; a linha liberada volta para as
    camadas seguintes, e se ninguém a reclamar é pendência — a certa.
    """
    for i, g in enumerate(conciliados):
        if g.tipo not in (CONCILIADO, DATA_DIFERENTE):
            continue
        (r,), (e,) = g.razao, g.extrato
        if r.data != pagamento.data or -r.valor != pagamento.valor:
            continue
        if _e_do_favorecido(r.historico, pagamento.favorecido) and not _e_do_favorecido(
            e.historico, pagamento.favorecido
        ):
            del conciliados[i]
            livres_e[e.id] = e
            return r
    return None


def _lotes_sispag(sispag, livres_r, livres_e, fechar, conciliados, pendencias) -> None:
    lotes: dict[tuple[date, str], list[PagamentoSispag]] = defaultdict(list)
    for p in sispag:
        if p.efetuado:
            lotes[(p.data, p.tipo)].append(p)

    # Boleto e PIX saem um por linha e já casaram na camada 1; aqui só o que o
    # banco juntou. O maior lote do dia primeiro: ele não pode perder a linha
    # para um lote menor que por acaso some o mesmo.
    ordem = sorted(lotes.items(), key=lambda kv: (kv[0][0], -sum(p.valor for p in kv[1])))
    for (dia, _tipo), pagamentos in ordem:
        if len(pagamentos) < 2:
            continue
        total = sum((p.valor for p in pagamentos), Decimal("0"))
        linhas = sorted(
            (e for e in livres_e.values() if e.data == dia and e.valor < 0),
            key=lambda e: e.id,
        )
        alvo = _linhas_do_lote(linhas, total)
        if alvo is None:
            continue

        por_valor: dict[Decimal, list[Item]] = defaultdict(list)
        for r in livres_r.values():
            if r.data == dia and r.valor < 0:
                por_valor[-r.valor].append(r)
        usados: list[Item] = []
        faltando: list[PagamentoSispag] = []
        for p in sorted(pagamentos, key=lambda p: -p.valor):
            opcoes = por_valor.get(p.valor)
            if not opcoes:
                recuperado = _recuperar_do_par_simples(p, conciliados, livres_e)
                if recuperado is not None:
                    usados.append(recuperado)
                else:
                    faltando.append(p)
                continue
            # Mesmo valor para dois favorecidos: decide o nome.
            melhor = max(opcoes, key=lambda r, p=p: _semelhanca(r.historico, p.favorecido))
            opcoes.remove(melhor)
            usados.append(melhor)

        if faltando:
            grupo = fechar(LOTE_SISPAG_DIVERGENTE, usados, alvo, pendencias)
            grupo.sispag_faltando = faltando
        else:
            fechar(LOTE_SISPAG, usados, alvo, conciliados)


def conciliar(
    razao: list[Item],
    extrato: list[Item],
    *,
    periodo_inicio: date | None = None,
    tolerancia_dias: int = 3,
    sispag: list[PagamentoSispag] | None = None,
) -> Resultado:
    abertura = [
        r for r in razao
        if _ABERTURA.search(r.historico) and (periodo_inicio is None or r.data == periodo_inicio)
    ]
    ids_abertura = {r.id for r in abertura}
    especies_no_extrato = {especie_de_aplicacao(e.historico, e.valor) for e in extrato} - {None}
    aplicacao_sem_extrato = [
        r for r in razao
        if r.id not in ids_abertura
        and (especie := especie_de_aplicacao(r.historico, r.valor)) is not None
        and especie not in especies_no_extrato
    ]
    ids_fora = ids_abertura | {r.id for r in aplicacao_sem_extrato}
    livres_r = {r.id: r for r in razao if r.id not in ids_fora}
    livres_e = {e.id: e for e in extrato}
    conciliados: list[Grupo] = []
    pendencias: list[Grupo] = []

    def _fechar(tipo: str, rs: list[Item], es: list[Item], destino: list[Grupo]) -> Grupo:
        for r in rs:
            livres_r.pop(r.id, None)
        for e in es:
            livres_e.pop(e.id, None)
        grupo = Grupo(tipo, list(rs), list(es))
        destino.append(grupo)
        return grupo

    # 1) mesmo dia, mesmo valor — desempate pelo histórico
    por_chave_r, por_chave_e = defaultdict(list), defaultdict(list)
    for r in livres_r.values():
        por_chave_r[(r.data, r.valor)].append(r)
    for e in livres_e.values():
        por_chave_e[(e.data, e.valor)].append(e)
    for chave, rs in por_chave_r.items():
        es = por_chave_e.get(chave)
        if es:
            for r, e in _parear_por_semelhanca(rs, es):
                _fechar(CONCILIADO, [r], [e], conciliados)

    # 2) mesmo valor, datas próximas — a mais próxima primeiro
    if tolerancia_dias > 0:
        por_valor = defaultdict(list)
        for e in livres_e.values():
            por_valor[e.valor].append(e)
        candidatos = sorted(
            (
                (abs((e.data - r.data).days), -_semelhanca(r.historico, e.historico), r.id, e.id)
                for r in livres_r.values()
                for e in por_valor.get(r.valor, [])
                if abs((e.data - r.data).days) <= tolerancia_dias
            )
        )
        for _, _, rid, eid in candidatos:
            if rid in livres_r and eid in livres_e:
                _fechar(DATA_DIFERENTE, [livres_r[rid]], [livres_e[eid]], conciliados)

    # 3) vários de um lado somam um do outro, no mesmo dia e sentido
    def _agrupar(fonte: dict, alvo: dict, lado_fonte_e_razao: bool) -> None:
        por_dia = defaultdict(list)
        for x in fonte.values():
            por_dia[(x.data, x.valor > 0)].append(x)
        for a in sorted(alvo.values(), key=lambda x: -abs(x.valor)):
            if a.id not in alvo:
                continue
            cands = [x for x in por_dia.get((a.data, a.valor > 0), [])
                     if x.id in fonte and abs(x.valor) <= abs(a.valor)]
            if len(cands) < 2 or len(cands) > _MAX_CANDIDATOS_AGRUPAMENTO:
                continue
            for k in range(2, min(_MAX_ITENS_AGRUPADOS, len(cands)) + 1):
                comb = next((c for c in itertools.combinations(cands, k)
                             if sum(x.valor for x in c) == a.valor), None)
                if comb:
                    if lado_fonte_e_razao:
                        _fechar(AGRUPADO, list(comb), [a], conciliados)
                    else:
                        _fechar(AGRUPADO, [a], list(comb), conciliados)
                    break

    _agrupar(livres_r, livres_e, lado_fonte_e_razao=True)
    _agrupar(livres_e, livres_r, lado_fonte_e_razao=False)

    # 3a) lote do SISPAG: pagamentos de um dia e tipo que somam linha(s) do extrato
    if sispag:
        _lotes_sispag(sispag, livres_r, livres_e, _fechar, conciliados, pendencias)

    # 3b) a sobra inteira do dia, num sentido, fecha com a sobra do extrato
    sobra_r, sobra_e = defaultdict(list), defaultdict(list)
    for r in livres_r.values():
        sobra_r[(r.data, r.valor > 0)].append(r)
    for e in livres_e.values():
        sobra_e[(e.data, e.valor > 0)].append(e)
    for chave in sorted(sobra_r.keys() & sobra_e.keys()):
        rs, es = sobra_r[chave], sobra_e[chave]
        # 1 × 1 com a mesma soma a camada 1 já teria casado; exigir 3 itens deixa
        # isto para o que é lote de fato.
        if len(rs) + len(es) >= 3 and sum(r.valor for r in rs) == sum(e.valor for e in es):
            _fechar(LOTE_DO_DIA, rs, es, conciliados)

    # 4) duplicidade: sobra que repete dia e valor de um lançamento já conciliado
    ja_conciliado = defaultdict(list)
    for g in conciliados:
        if g.tipo == CONCILIADO:
            ja_conciliado[(g.razao[0].data, g.razao[0].valor)].append(g)
    for r in sorted(livres_r.values(), key=lambda x: (x.data, x.historico)):
        pares = ja_conciliado.get((r.data, r.valor))
        if pares:
            _fechar(DUPLICIDADE_RAZAO, [r], [pares[0].extrato[0]], pendencias)
            livres_e.pop(pares[0].extrato[0].id, None)  # já estava fora; mantém a referência
    for e in sorted(livres_e.values(), key=lambda x: (x.data, x.historico)):
        pares = ja_conciliado.get((e.data, e.valor))
        if pares:
            _fechar(DUPLICIDADE_EXTRATO, [pares[0].razao[0]], [e], pendencias)

    # 5) mesmo dia, valor muito próximo
    divergencias = sorted(
        (
            (abs(r.valor - e.valor), r.id, e.id)
            for r in livres_r.values()
            for e in livres_e.values()
            if r.data == e.data and (r.valor > 0) == (e.valor > 0) and r.valor != e.valor
            and abs(r.valor - e.valor) <= min(_DIVERGENCIA_MAX_ABSOLUTA, abs(e.valor) * _DIVERGENCIA_MAX_RELATIVA)
        )
    )
    for _, rid, eid in divergencias:
        if rid in livres_r and eid in livres_e:
            _fechar(VALOR_DIVERGENTE, [livres_r[rid]], [livres_e[eid]], pendencias)

    # 6) o que sobrou
    for r in sorted(livres_r.values(), key=lambda x: (x.data, -abs(x.valor))):
        pendencias.append(Grupo(SO_RAZAO, [r], []))
    for e in sorted(livres_e.values(), key=lambda x: (x.data, -abs(x.valor))):
        pendencias.append(Grupo(SO_EXTRATO, [], [e]))

    return Resultado(
        conciliados=conciliados,
        pendencias=pendencias,
        abertura=abertura,
        aplicacao_sem_extrato=aplicacao_sem_extrato,
    )
