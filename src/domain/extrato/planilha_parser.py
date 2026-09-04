"""Extrato bancário em planilha (.xls, .xlsx, .csv).

POR QUE ISTO EXISTE

Até aqui o importador tratava `.pdf` e mandava TODO o resto para o parser de
OFX. Planilha era aceita no upload e falhava com "OFX inválido" — mensagem que
manda procurar problema no arquivo errado.

Na pasta de extratos do escritório são 88 planilhas, e **36 delas não têm PDF
nem OFX irmão**: são extratos que o sistema não lia em formato nenhum.

A PLANILHA NOMEIA AS PRÓPRIAS COLUNAS — E ISSO MUDA O DESENHO

No PDF, descobrir se um número é débito ou crédito exige coordenada (Sicredi),
posição do traço (Daycoval) ou o sinal no próprio valor (Viacredi), e cada banco
pede um adaptador. Aqui o cabeçalho declara:

    Data | Lançamento | Dcto. | Crédito (R$) | Débito (R$) | Saldo (R$)
    Data | Descrição | Documento | Valor (R$) | Saldo (R$)
    Data | Lançamento | Razão Social | CPF/CNPJ | Valor (R$) | Saldo (R$)

Então UM leitor guiado por nome de coluna cobre os bancos todos, e um layout
novo entra sem código — desde que use nomes reconhecíveis. É o inverso do
`bancos/`, e de propósito.

O CNPJ VEM EM COLUNA PRÓPRIA, E ISSO É MELHOR QUE O PDF

O Itaú entrega `Razão Social` e `CPF/CNPJ` separados e preenchidos. No PDF esses
dois campos vivem dentro do texto corrido, e é por isso que existem o
`documento_no_historico.py` (três rodadas de regex) e o `contraparte_por_nome.py`
(que falha quando o banco trunca o nome).

Aqui eles entram no histórico gerado, na ordem em que o extrato os imprime. Não
há campo estruturado novo: o `documento_no_historico` já acha o CNPJ dentro do
texto, e a contraparte resolve pela via que já existe — sem migration e sem
mexer no motor.

O QUE NÃO MUDA

O resultado é `list[TransacaoOFX]`, igual ao do PDF, e passa pela MESMA cadeia
de saldos. Planilha ser tabulada não a torna confiável: `SALDO ANTERIOR` continua
sendo linha de abertura e não lançamento, e o total no rodapé continua tendo a
forma de um — ver [[extrato-nao-termina-no-ultimo-lancamento]].
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

EXTENSOES = (".xls", ".xlsx", ".xlsm", ".csv")

# O mesmo centavo de folga que o caminho do PDF usa. Vale para os dois porque a
# origem do desvio é a mesma — arredondamento na impressão do saldo —, e não a
# forma do arquivo.
TOLERANCIA_SALDO = Decimal("0.05")


class PlanilhaParseError(Exception):
    pass


def _sem_acento(texto: str) -> str:
    normal = unicodedata.normalize("NFD", texto)
    return "".join(c for c in normal if unicodedata.category(c) != "Mn")


def _chave(valor: object) -> str:
    """Forma canônica de um rótulo de coluna: sem acento, sem pontuação, minúsculo."""
    if valor is None:
        return ""
    texto = _sem_acento(str(valor)).lower()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


# Cada papel e os rótulos que o identificam, do mais específico para o mais
# genérico. A ordem importa: "data lancamento" tem de ser testado antes de
# "data", senão a Caixa (que tem duas colunas de data) casa na errada.
_PAPEIS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("credito", ("credito", "entrada", "entradas")),
    ("debito", ("debito", "saida", "saidas")),
    ("saldo", ("saldo",)),
    ("valor", ("valor lancamento", "valor r", "valor")),
    ("data", ("data lancamento", "data movimento", "data")),
    ("documento", ("documento", "dcto", "dcto n", "n documento", "identificador")),
    ("razao_social", ("razao social", "cliente ou fornecedor", "favorecido")),
    ("cpf_cnpj", ("cpf cnpj", "cnpj cpf", "cpf", "cnpj")),
    ("historico", ("lancamento", "descricao", "historico")),
)

# Rótulo da linha de abertura. Não é lançamento: é o saldo de onde a cadeia parte.
_ABERTURA = re.compile(r"saldo\s+anterior|saldo\s+inicial", re.IGNORECASE)
# Linhas de fecho e totalização, que têm a forma de um lançamento e não são um.
_FECHO = re.compile(
    r"^(saldo\s+(atual|final|do\s+dia|em\s)|total(\s|$)|lan[cç]amentos?\s+futuros)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Linha:
    """Uma linha de movimento já lida, antes de virar `TransacaoOFX`.

    Existe porque três correções — ordem, quebra em blocos e sinal — só podem
    ser feitas olhando a planilha inteira, e não linha a linha.
    """

    data: date
    valor: Decimal
    saldo: Decimal | None
    historico: str


def _decrescente(linhas: list[_Linha]) -> bool:
    """A planilha lista do mais recente para o mais antigo?

    O Santander exporta assim, e a cadeia de saldos lida na ordem do arquivo
    andava para trás em todos os 29 lançamentos. Lida de baixo para cima ela
    fecha exata — não havia valor errado, só ordem invertida.

    A pergunta é sobre a sequência de datas DISTINTAS: dentro do mesmo dia a
    ordem é arbitrária e não diz nada. Exige-se pelo menos duas datas e que a
    primeira seja maior que a última, senão um arquivo de um dia só ou já
    ordenado seria invertido à toa.
    """
    datas = [linha.data for linha in linhas]
    distintas = [d for i, d in enumerate(datas) if i == 0 or d != datas[i - 1]]
    if len(distintas) < 2 or distintas[0] <= distintas[-1]:
        return False
    return all(a >= b for a, b in zip(distintas, distintas[1:]))


def _ajustar_pelo_saldo(saldo_anterior: Decimal | None, linhas: list[_Linha]) -> list[_Linha]:
    """Usa a coluna de saldo para corrigir o sinal e descartar o que não é movimento.

    CORRIGIR O SINAL — quando só o saldo diz a direção.

    O extrato de poupança do Sicredi imprime a coluna `Valor (R$)` SEM sinal:

        23/02/2026 | CAPITALIZ. REND. CM |  0,54 | 108,34
        23/02/2026 | ENCARGOS DE IRRF    |  0,48 | 107,86

    O IRRF é um débito impresso positivo. Nada no valor, no nome da coluna ou
    na posição distingue os dois — só o saldo, que cai de 108,34 para 107,86.

    A troca só acontece quando o saldo concorda com o MÓDULO e discorda do
    sinal. Se a coluna de saldo estivesse errada, a magnitude também estaria, e
    a linha ficaria como veio: a correção não tem como inventar um valor que a
    planilha não trouxe, só escolher entre `+x` e `−x` com a cadeia decidindo.

    DESCARTAR O QUE NÃO É MOVIMENTO — quando o saldo não anda.

    O relatório do Omie.CASH não é extrato: é fluxo de caixa, e traz previsões
    junto do que aconteceu. A última linha de julho/2026 é um pedido de venda
    `Atrasado` de R$ 72.500 que ainda não entrou:

        -2.230,00 | Saldo 20.804,40 | Previsto 442.885,06
            -0,50 | Saldo 20.803,90 | Previsto 442.884,56
        72.500,00 | Saldo 20.803,90 | Previsto 515.384,56   ← não entrou

    Só a coluna `Saldo Previsto` se mexe. Uma linha com valor não-zero que
    deixa o saldo onde estava não movimentou a conta — isso é aritmética, não
    palpite, e vale para qualquer sistema que misture previsto e realizado.
    É a mesma família do rodapé do Itaú lido como crédito de R$ 19.070,30.
    """
    ajustadas: list[_Linha] = []
    anterior = saldo_anterior
    for linha in linhas:
        atual = linha
        if anterior is not None and linha.saldo is not None:
            delta = linha.saldo - anterior
            if delta == 0:
                continue
            if delta != linha.valor and abs(delta) == abs(linha.valor):
                atual = replace(linha, valor=delta)
        ajustadas.append(atual)
        if atual.saldo is not None:
            anterior = atual.saldo
        elif anterior is not None:
            anterior = anterior + atual.valor
    return ajustadas


def _mapear_colunas(linha: list[object]) -> dict[str, int] | None:
    """Papel de cada coluna, a partir dos rótulos do cabeçalho.

    Devolve `None` se a linha não parece um cabeçalho — precisa ter data e ao
    menos uma coluna de dinheiro, senão qualquer linha de texto viraria
    cabeçalho e o resto do arquivo seria lido torto.
    """
    mapa: dict[str, int] = {}
    for indice, celula in enumerate(linha):
        rotulo = _chave(celula)
        if not rotulo:
            continue
        for papel, aliases in _PAPEIS:
            if papel in mapa:
                continue
            if any(rotulo == a or rotulo.startswith(a + " ") for a in aliases):
                mapa[papel] = indice
                break

    tem_dinheiro = {"valor", "credito", "debito"} & mapa.keys()
    if "data" in mapa and tem_dinheiro:
        return mapa
    return None


# A célula da coluna de data tem de SER uma data, não conter uma.
#
# O `parse_data` acha data em qualquer ponto do texto, o que é certo para o PDF,
# onde a linha é corrida. Aqui a coluna já foi identificada pelo cabeçalho, e
# essa liberdade só faz mal: no extrato de poupança do Sicredi a linha
# `Saldo a partir 04/05/12:` fica na coluna de data, e virou um lançamento de
# R$ 107,86 em 2012. Pior que o lançamento inventado foi o efeito colateral —
# aquele 2012 no meio de datas de 2026 fez a planilha parecer decrescente, e o
# arquivo inteiro foi invertido.
_SO_DATA = re.compile(
    r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)?$"
)


def _para_data(valor: object, referencia_ano: int) -> date | None:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if valor is None:
        return None
    texto = str(valor).strip()
    if not _SO_DATA.match(texto):
        return None
    # `AAAA-MM-DD`, que o `parse_data` não lê: ele atende o PDF, onde a data vem
    # sempre no formato brasileiro. O Grafeno imprime ISO com hora na mesma
    # coluna em que imprime `31/07/2026`, e num CSV essas células chegam como
    # texto, sem o tipo da planilha para desempatar. Ampliar o `parse_data`
    # mexeria em todos os adaptadores de PDF por causa de um caso de planilha.
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", texto)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    return parse_data(texto, referencia_ano)


def _para_decimal(valor: object) -> Decimal | None:
    """Número da célula, venha ele tipado ou como texto no formato brasileiro."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return Decimal(str(valor))
    if isinstance(valor, Decimal):
        return valor
    return parse_valor(str(valor))


def _linhas_xlsx(conteudo: bytes) -> list[list[object]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    try:
        planilha = wb.worksheets[0]
        # O xlsx do Banco do Brasil declara a dimensão errada — diz ter uma
        # linha e tem 447. Em `read_only` o openpyxl acredita no que está
        # declarado e para na primeira, sem erro nenhum: o extrato chegava
        # vazio e a recusa dizia "sem cabeçalho reconhecível".
        planilha.reset_dimensions()
        return [list(linha) for linha in planilha.iter_rows(values_only=True)]
    finally:
        wb.close()


def _linhas_xls(conteudo: bytes) -> list[list[object]]:
    import xlrd

    wb = xlrd.open_workbook(file_contents=conteudo)
    ws = wb.sheet_by_index(0)
    linhas: list[list[object]] = []
    for i in range(ws.nrows):
        atual: list[object] = []
        for celula in ws.row(i):
            # Data em .xls é número de série; só o tipo da célula diz.
            if celula.ctype == xlrd.XL_CELL_DATE:
                atual.append(datetime(*xlrd.xldate_as_tuple(celula.value, wb.datemode)))
            else:
                atual.append(celula.value)
        linhas.append(atual)
    return linhas


def _linhas_csv(conteudo: bytes) -> list[list[object]]:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            texto = conteudo.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            dialeto = csv.Sniffer().sniff(texto[:4096], delimiters=";,\t")
        except csv.Error:
            # Sem amostra suficiente para farejar; ponto e vírgula é o padrão
            # dos bancos brasileiros.
            dialeto = csv.excel
            dialeto.delimiter = ";"
        # `newline=""` é exigência do módulo `csv`, não gosto: sem ele o
        # terminador de linha chega inteiro dentro do campo e o leitor para com
        # "new-line character seen in unquoted field" — foi o que recusou o
        # `Bradesco_18052026_141344.CSV`, 781 linhas sem uma única aspa.
        return [
            list(linha)
            for linha in csv.reader(io.StringIO(texto, newline=""), dialeto)
        ]
    raise PlanilhaParseError("Não foi possível decodificar o arquivo CSV.")


def _ler_linhas(conteudo: bytes, nome_arquivo: str) -> list[list[object]]:
    nome = (nome_arquivo or "").lower()
    if nome.endswith(".csv"):
        return _linhas_csv(conteudo)
    # `.xls` que na verdade é `.xlsx` acontece: o banco renomeia a extensão e o
    # xlrd recusa com "Excel xsx file; not supported". A assinatura ZIP no
    # início do arquivo é o que decide de verdade.
    if conteudo[:2] == b"PK":
        return _linhas_xlsx(conteudo)
    if nome.endswith((".xlsx", ".xlsm")):
        return _linhas_xlsx(conteudo)
    return _linhas_xls(conteudo)


def _ler_blocos(
    conteudo: bytes, nome_arquivo: str, referencia_ano: int | None = None
) -> list[Bloco]:
    """Lê a planilha e devolve os blocos já em ordem crescente e com sinal certo."""
    referencia_ano = referencia_ano or datetime.now().year

    try:
        linhas = _ler_linhas(conteudo, nome_arquivo)
    except PlanilhaParseError:
        raise
    except Exception as exc:
        raise PlanilhaParseError(
            f"Não foi possível abrir a planilha: {exc}"
        ) from exc

    mapa: dict[str, int] | None = None
    inicio = 0
    for indice, linha in enumerate(linhas):
        mapa = _mapear_colunas(linha)
        if mapa:
            inicio = indice + 1
            break

    if not mapa:
        raise PlanilhaParseError(
            "A planilha não tem uma linha de cabeçalho reconhecível. "
            "Esperado ao menos uma coluna de data e uma de valor, crédito ou débito."
        )

    def celula(linha: list[object], papel: str) -> object:
        indice = mapa.get(papel)
        if indice is None or indice >= len(linha):
            return None
        return linha[indice]

    # Uma planilha pode trazer mais de uma cadeia. O extrato consolidado do
    # Bradesco emenda dois períodos no mesmo arquivo, cada um abrindo com seu
    # `SALDO ANTERIOR` — na `BRADESCO C&C JUL 26.XLS` o segundo está na linha
    # 267 de 301. Conferir os dois como uma cadeia só acusa um salto no ponto de
    # emenda que não é erro nenhum; é o mesmo motivo pelo qual o `Bloco` existe.
    secoes: list[tuple[Decimal | None, list[_Linha]]] = [(None, [])]

    for linha in linhas[inicio:]:
        if not any(c not in (None, "") for c in linha):
            continue

        texto_historico = " ".join(
            str(celula(linha, papel)).strip()
            for papel in ("historico", "razao_social", "cpf_cnpj", "documento")
            if celula(linha, papel) not in (None, "")
        )

        if _ABERTURA.search(texto_historico):
            saldo = _para_decimal(celula(linha, "saldo"))
            if secoes[-1][1]:
                secoes.append((saldo, []))
            else:
                secoes[-1] = (saldo, secoes[-1][1])
            continue

        # O rodapé tem a forma de um lançamento e não é um — três incidentes
        # nesta base já entraram por aí.
        if _FECHO.match(texto_historico.strip()):
            continue

        data_lida = _para_data(celula(linha, "data"), referencia_ano)
        if data_lida is None:
            continue

        credito = _para_decimal(celula(linha, "credito"))
        debito = _para_decimal(celula(linha, "debito"))
        if credito is not None or debito is not None:
            # Colunas separadas: o sinal vem de QUAL delas recebeu o número, e
            # não do número — alguns bancos já imprimem o débito negativo, e
            # outros não.
            if credito is not None and credito != 0:
                valor = abs(credito)
            elif debito is not None and debito != 0:
                valor = -abs(debito)
            else:
                continue
        else:
            valor = _para_decimal(celula(linha, "valor"))

        if valor is None or valor == 0:
            continue

        historico = re.sub(r"\s+", " ", texto_historico).strip() or "SEM DESCRIÇÃO"
        secoes[-1][1].append(
            _Linha(
                data=data_lida,
                valor=valor,
                saldo=_para_decimal(celula(linha, "saldo")),
                historico=historico[:200],
            )
        )

    todas = [linha for _, linhas_da_secao in secoes for linha in linhas_da_secao]
    if not todas:
        raise PlanilhaParseError(
            "A planilha foi lida, mas nenhuma linha tinha data e valor ao mesmo "
            "tempo. Confira se o arquivo é um extrato de conta corrente."
        )

    # Ordem e quebra em blocos são decisões que se atrapalham. Numa planilha
    # decrescente o `SALDO ANTERIOR` de uma emenda ficaria ABAIXO das linhas a
    # que pertence, e o corte cairia no lugar errado — pior que não cortar. Não
    # há exemplo desse arquivo nesta base; até haver, decrescente vira bloco
    # único, e a cadeia acusa se algum dia chegar um com emenda.
    if _decrescente(todas):
        todas.reverse()
        secoes = [(None, todas)]

    blocos: list[Bloco] = []
    idx = 0
    for saldo_anterior, linhas_da_secao in secoes:
        if not linhas_da_secao:
            continue
        transacoes: list[TransacaoOFX] = []
        for atual in _ajustar_pelo_saldo(saldo_anterior, linhas_da_secao):
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(atual.data, atual.historico, atual.valor, idx),
                    data=atual.data,
                    valor=atual.valor,
                    historico=atual.historico,
                    tipo_ofx="CREDIT" if atual.valor > 0 else "DEBIT",
                    saldo_apos=atual.saldo,
                    ordem=idx,
                )
            )
            idx += 1
        blocos.append(Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior))

    return blocos


def parse_planilha(
    conteudo: bytes, nome_arquivo: str, referencia_ano: int | None = None
) -> list[TransacaoOFX]:
    """Lê um extrato em planilha e devolve as transações em ordem cronológica.

    Confere a cadeia de saldos antes de devolver, igual ao `parse_pdf`. Sem
    isso, uma coluna lida torto entraria calada — e planilha dá exatamente a
    falsa sensação de que isso não pode acontecer, por já vir tabulada. Nesta
    base a conferência reprovou 15 arquivos de 56, e nos 15 o erro era meu:
    ordem invertida, emenda de dois períodos e valor sem sinal.
    """
    from src.domain.extrato.pdf_parser import PDFParseError, _validar_blocos

    blocos = _ler_blocos(conteudo, nome_arquivo, referencia_ano)
    try:
        _validar_blocos(blocos, TOLERANCIA_SALDO)
    except PDFParseError as exc:
        # A conferência é a mesma do PDF e levanta o erro de lá. A frase serve
        # nos dois casos; o nome da classe, não — quem importa uma planilha e
        # recebe `PDFParseError` vai procurar defeito no arquivo errado.
        raise PlanilhaParseError(str(exc)) from exc
    return [transacao for bloco in blocos for transacao in bloco.transacoes]


def blocos_da_planilha(
    conteudo: bytes, nome_arquivo: str, referencia_ano: int | None = None
) -> list[Bloco]:
    """Mesma leitura, em `Bloco`, para a conferência de saldos."""
    return _ler_blocos(conteudo, nome_arquivo, referencia_ano)
