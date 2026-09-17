"""Leitura do razão contábil de UMA conta banco, em planilha ou PDF.

É o mesmo relatório "RAZÃO" do Mister Contador que o ConcilPro lê para
fornecedores — blocos "Conta:", colunas Data/Lote/Histórico/Cta.C.Part./
Débito/Crédito/Saldo e "Total da conta" no rodapé. O razão de uma conta banco é
esse arquivo com um bloco só.

PLANILHA: o leitor do ConcilPro, sem mudança. Medido no razão da conta 2699 da
BLD (fev/2025): 3.280 lançamentos, fechando com o "Total da conta".

PDF: leitura por TEXTO, e não pelo caminho do ConcilPro. Lá a IA (Vision) é o
leitor principal porque o razão de fornecedores em PDF é irregular; o razão de
uma conta banco não é, e o texto dá exatamente os mesmos 3.280 lançamentos da
planilha — sem custo, sem tempo de IA e reproduzível.

A CONFERÊNCIA QUE NÃO DEIXA LER PELA METADE

A soma dos débitos e dos créditos lidos tem de bater com o "Total da conta"
impresso. Se não bate, algum lançamento ficou para trás, e conciliar em cima
disso apontaria pendências que não existem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

TOLERANCIA = Decimal("0.01")


class RazaoInvalido(Exception):
    """O arquivo não é um razão de uma conta legível e conferido."""


@dataclass(frozen=True)
class LancamentoRazao:
    data: date
    lote: str
    historico: str
    contrapartida: str
    # Visto do banco: débito na conta banco é entrada (+), crédito é saída (−).
    valor: Decimal


@dataclass
class RazaoConta:
    empresa: str
    cnpj: str
    periodo_inicio: date | None
    periodo_fim: date | None
    conta_codigo: str          # código reduzido do Mister Contador — "2699"
    conta_classificacao: str   # "1.1.1.02.0015"
    conta_descricao: str       # "BANCO ITAÚ C/C 122870"
    lancamentos: list[LancamentoRazao] = field(default_factory=list)
    total_debito: Decimal = Decimal("0")
    total_credito: Decimal = Decimal("0")


def _so_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


def ler_razao(conteudo: bytes, nome_arquivo: str = "") -> RazaoConta:
    if conteudo[:4] == b"%PDF":
        razao = _ler_pdf(conteudo)
    elif conteudo[:2] == b"PK" or conteudo[:4] == b"\xd0\xcf\x11\xe0":
        razao = _ler_planilha(conteudo)
    else:
        raise RazaoInvalido("Formato não reconhecido. Envie o razão em PDF, XLSX ou XLS.")
    if not razao.lancamentos:
        raise RazaoInvalido("O razão foi lido, mas não tem nenhum lançamento no período.")
    _conferir_total(razao)
    return razao


def _conferir_total(razao: RazaoConta) -> None:
    debito = sum((l.valor for l in razao.lancamentos if l.valor > 0), Decimal("0"))
    credito = -sum((l.valor for l in razao.lancamentos if l.valor < 0), Decimal("0"))
    if abs(debito - razao.total_debito) > TOLERANCIA or abs(credito - razao.total_credito) > TOLERANCIA:
        raise RazaoInvalido(
            "A leitura do razão não fecha com o \"Total da conta\" impresso "
            f"(débitos lidos {debito:,.2f} × {razao.total_debito:,.2f}; créditos lidos "
            f"{credito:,.2f} × {razao.total_credito:,.2f}). Algum lançamento não foi lido — "
            "conciliar assim apontaria pendências que não existem. Se possível, envie o "
            "razão em planilha."
        )


# ── planilha ────────────────────────────────────────────────────────────────

def _ler_planilha(conteudo: bytes) -> RazaoConta:
    from src.domain.concilpro.planilha import parsear_planilha_razao, parsear_xls_razao

    try:
        if conteudo[:2] == b"PK":
            dados = parsear_planilha_razao(conteudo)
        else:
            dados = parsear_xls_razao(conteudo)
    except ValueError as exc:
        raise RazaoInvalido(str(exc)) from exc

    blocos = dados["fornecedores"]
    _exigir_uma_conta(len(blocos))
    bloco = blocos[0]

    def _data(valor) -> date | None:
        return valor.date() if isinstance(valor, datetime) else valor

    return RazaoConta(
        empresa=dados.get("empresa") or "",
        cnpj=_so_digitos(dados.get("cnpj")),
        periodo_inicio=_data(dados.get("periodo_inicio")),
        periodo_fim=_data(dados.get("periodo_fim")),
        conta_codigo=str(bloco.get("codigo_conta") or ""),
        conta_classificacao=str(bloco.get("conta_contabil") or ""),
        conta_descricao=str(bloco.get("nome_fornecedor") or ""),
        lancamentos=[
            LancamentoRazao(
                data=_data(l["data_lancamento"]),
                lote=str(l.get("lote") or ""),
                historico=l.get("historico") or "",
                contrapartida=str(l.get("conta_partida") or ""),
                valor=(Decimal(l["valor_debito"]) - Decimal(l["valor_credito"])).quantize(TOLERANCIA),
            )
            for l in bloco["lancamentos"]
        ],
        total_debito=Decimal(bloco["total_debito"]),
        total_credito=Decimal(bloco["total_credito"]),
    )


def _exigir_uma_conta(quantidade: int) -> None:
    if quantidade == 0:
        raise RazaoInvalido("Não encontrei nenhuma conta (linha \"Conta:\") no razão.")
    if quantidade > 1:
        raise RazaoInvalido(
            f"O razão tem {quantidade} contas. A conciliação é de uma conta bancária por vez: "
            "extraia o razão só da conta deste banco."
        )


# ── PDF ─────────────────────────────────────────────────────────────────────

_NUM = r"\d{1,3}(?:\.\d{3})*,\d{2}"
# O saldo zerado sai sem D/C ("... 0,59 0,00"). Exigir o sufixo perdia a linha,
# e o valor dela ia parar no lançamento seguinte, que é calculado pela diferença
# de saldo: no razão da aplicação da BLD (2025) eram 17 linhas e R$ 550.668,30.
_LANCAMENTO = re.compile(
    rf"^(\d{{2}}/\d{{2}}/\d{{4}})\s+(\d+)\s+(.+?)\s+(?:(\d{{1,6}})\s+)?({_NUM})\s+({_NUM})([CD]?)$"
)
_CONTA = re.compile(r"^Conta:\s*(\d+)\s*-\s*([\d.]+)\s+(.+)$")
_PERIODO = re.compile(r"Per[ií]odo:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")
_SALDO_ANTERIOR = re.compile(rf"SALDO ANTERIOR\s+({_NUM})([CD])?\s*$")
_TOTAL = re.compile(rf"^Total da conta:\s*({_NUM})\s+({_NUM})")
# O cabeçalho se repete em toda página, e o razão de vários meses fecha cada um
# com "Total do mês"; nada disso pode virar continuação de histórico.
_CABECALHO = re.compile(
    r"^(Empresa:|C\.N\.P\.J\.:|Per[ií]odo:|CONSOLIDADO|RAZ[ÃA]O$|Data\s*Lote|Conta:|SALDO ANTERIOR"
    r"|Total da conta|Total do m[êe]s|Sistema licenciado|_{5,})",
    re.IGNORECASE,
)


def _dec(texto: str) -> Decimal:
    return Decimal(texto.replace(".", "").replace(",", "."))


def _ler_pdf(conteudo: bytes) -> RazaoConta:
    import pdfplumber

    try:
        with pdfplumber.open(BytesIO(conteudo)) as pdf:
            linhas = [l.strip() for p in pdf.pages for l in (p.extract_text() or "").splitlines()]
    except Exception as exc:
        raise RazaoInvalido(f"Não foi possível abrir o PDF: {exc}") from exc
    return _razao_das_linhas(linhas)


def _razao_das_linhas(linhas: list[str]) -> RazaoConta:
    razao = RazaoConta(empresa="", cnpj="", periodo_inicio=None, periodo_fim=None,
                       conta_codigo="", conta_classificacao="", conta_descricao="")
    contas: set[str] = set()
    saldo: Decimal | None = None
    historicos: list[list[str]] = []
    brutos: list[tuple] = []
    total: tuple[Decimal, Decimal] | None = None

    for linha in linhas:
        if not linha:
            continue
        if linha.startswith("Empresa:") and not razao.empresa:
            razao.empresa = re.sub(r"\s+Folha:.*$", "", linha[len("Empresa:"):]).strip()
            continue
        if linha.startswith("C.N.P.J.") and not razao.cnpj:
            razao.cnpj = _so_digitos(linha.split(":", 1)[-1])
            continue
        m = _PERIODO.search(linha)
        if m and razao.periodo_inicio is None:
            razao.periodo_inicio = datetime.strptime(m.group(1), "%d/%m/%Y").date()
            razao.periodo_fim = datetime.strptime(m.group(2), "%d/%m/%Y").date()
            continue
        m = _CONTA.match(linha)
        if m:
            contas.add(m.group(1))
            razao.conta_codigo, razao.conta_classificacao, razao.conta_descricao = (
                m.group(1), m.group(2), m.group(3).strip())
            continue
        m = _SALDO_ANTERIOR.search(linha)
        if m and saldo is None:
            valor = _dec(m.group(1))
            saldo = -valor if m.group(2) == "C" else valor
            continue
        m = _TOTAL.match(linha)
        if m:
            total = (_dec(m.group(1)), _dec(m.group(2)))
            continue
        if total is not None:
            # Depois do total só vêm os termos e as assinaturas do fechamento.
            continue
        m = _LANCAMENTO.match(linha)
        if m:
            brutos.append(m.groups())
            historicos.append([m.group(3)])
            continue
        if historicos and not _CABECALHO.match(linha):
            historicos[-1].append(linha)  # histórico quebrado em mais de uma linha

    _exigir_uma_conta(len(contas))
    if total is None:
        raise RazaoInvalido("Não encontrei o \"Total da conta\" no PDF, e sem ele a leitura não pode ser conferida.")
    razao.total_debito, razao.total_credito = total

    # O PDF não diz em que coluna o valor está: é o saldo que diz. Débito na
    # conta banco aumenta o saldo devedor (D); crédito diminui.
    saldo = saldo or Decimal("0")
    for (data_txt, lote, _hist, contra, _valor, saldo_txt, dc), partes in zip(brutos, historicos):
        novo = _dec(saldo_txt) * (1 if dc == "D" else -1)
        razao.lancamentos.append(LancamentoRazao(
            data=datetime.strptime(data_txt, "%d/%m/%Y").date(),
            lote=lote,
            historico=" ".join(partes),
            contrapartida=contra or "",
            valor=(novo - saldo).quantize(TOLERANCIA),
        ))
        saldo = novo
    return razao
