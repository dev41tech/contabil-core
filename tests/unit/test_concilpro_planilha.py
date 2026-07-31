"""Testes unitários — parser do Razão em planilha (XLSX).

As planilhas de teste são montadas em memória com openpyxl: os razões reais
disponíveis contêm CNPJ e razão social de clientes e não devem ir para o repo.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest

from src.domain.concilpro.parser import detectar_formato_arquivo
from src.domain.concilpro.planilha import (
    e_planilha,
    localizar_colunas,
    parsear_planilha_razao,
)

# Layout real do export: Data | Lote | Histórico | ... | Cta.C.Part. | Débito | Crédito | ... | Saldo
_CABECALHO = ["Data", "Lote", "Histórico", "", "", "", "",
              "Cta.C.Part.", "Débito", "Crédito", "", "", "Saldo-Exercício"]


def _linha(**campos) -> list:
    linha = [""] * 13
    for chave, indice in (("data", 0), ("lote", 1), ("historico", 2),
                          ("cta", 7), ("debito", 8), ("credito", 9), ("saldo", 12)):
        if chave in campos:
            linha[indice] = campos[chave]
    return linha


def _planilha(linhas: list[list], cabecalho: list | None = None) -> bytes:
    """Monta um XLSX em memória com o preâmbulo do razão."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Razão"
    ws.append(["Empresa:", "", "TRANSPORTES TESTE LTDA", "", "", "", "", "", "", "", "", "Folha:", "", 1])
    ws.append(["C.N.P.J.:", "", "12.345.678/0001-90"])
    ws.append(["Período:", "", "01/01/2025 - 31/12/2025"])
    ws.append([])
    ws.append(["RAZÃO"])
    ws.append([])
    ws.append(cabecalho or _CABECALHO)
    for linha in linhas:
        ws.append(linha)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


_BLOCO_SIMPLES = [
    ["Conta:", 1670, "2.1.3.01.0005", "", "", "NORDICA VEICULOS S/A"],
    _linha(historico="SALDO ANTERIOR", saldo=-1088.77),
    _linha(data=datetime(2025, 2, 13), lote=20812, historico="VLR REF PGTO NORDICA",
           cta=1740, debito=217.75, saldo=-871.02),
    _linha(data=datetime(2025, 3, 10), lote=94, historico="COMPRAS CONFORME NF. Nº 116722",
           cta=609, credito=350.26, saldo=-1221.28),
    ["", "", "", "", "Total da conta:", "", "", "", 217.75, 350.26],
]


class TestDeteccaoDeFormato:
    def test_reconhece_xlsx(self):
        conteudo = _planilha(_BLOCO_SIMPLES)
        assert e_planilha(conteudo)
        assert detectar_formato_arquivo(conteudo) == "XLSX"

    def test_pdf_nao_e_planilha(self):
        assert not e_planilha(b"%PDF-1.7\nalgum conteudo")
        assert detectar_formato_arquivo(b"%PDF-1.7\nalgum conteudo") == "PDF"

    def test_zip_comum_nao_e_planilha(self):
        import zipfile
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("leiame.txt", "nao sou planilha")
        conteudo = buf.getvalue()
        # começa com PK, mas não tem a estrutura OpenXML
        assert conteudo[:2] == b"PK"
        assert not e_planilha(conteudo)
        assert detectar_formato_arquivo(conteudo) == "ZIP"


class TestLocalizarColunas:
    def test_localiza_pelo_rotulo(self):
        cols = localizar_colunas([tuple(_CABECALHO)])
        assert cols["debito"] == 8
        assert cols["credito"] == 9
        assert cols["historico"] == 2

    def test_aceita_numero_no_lugar_de_lote(self):
        """O mesmo sistema exporta 'Lote' num razão e 'Número' noutro."""
        cabecalho = list(_CABECALHO)
        cabecalho[1] = "Número"
        cols = localizar_colunas([tuple(cabecalho)])
        assert cols["lote"] == 1

    def test_indice_acompanha_a_ordem_das_colunas(self):
        """Índice fixo quebraria; o rótulo tem que mandar."""
        cabecalho = ["Data", "Lote", "Crédito", "Débito", "Histórico"]
        cols = localizar_colunas([tuple(cabecalho)])
        assert cols["credito"] == 2
        assert cols["debito"] == 3

    def test_sem_cabecalho_reconhecivel(self):
        assert localizar_colunas([("a", "b", "c")]) is None


class TestParsearPlanilha:
    def test_extrai_fornecedor_e_lancamentos(self):
        r = parsear_planilha_razao(_planilha(_BLOCO_SIMPLES))

        assert r["empresa"] == "TRANSPORTES TESTE LTDA"
        assert r["cnpj"] == "12.345.678/0001-90"
        assert r["periodo_inicio"] == datetime(2025, 1, 1)
        assert r["total_fornecedores"] == 1

        forn = r["fornecedores"][0]
        assert forn["codigo_conta"] == "1670"
        assert forn["conta_contabil"] == "2.1.3.01.0005"
        assert forn["nome_fornecedor"] == "NORDICA VEICULOS S/A"
        assert len(forn["lancamentos"]) == 2

    def test_debito_e_credito_vem_da_coluna(self):
        """Sem inferência: a coluna diz o que o valor é."""
        forn = parsear_planilha_razao(_planilha(_BLOCO_SIMPLES))["fornecedores"][0]
        pagamento, compra = forn["lancamentos"]

        assert pagamento["valor_debito"] == Decimal("217.75")
        assert pagamento["valor_credito"] == Decimal("0")
        assert pagamento["tipo_operacao"] == "PAGAMENTO"

        assert compra["valor_credito"] == Decimal("350.26")
        assert compra["valor_debito"] == Decimal("0")
        assert compra["tipo_operacao"] == "COMPRA"

    def test_totais_batem_com_o_declarado(self):
        forn = parsear_planilha_razao(_planilha(_BLOCO_SIMPLES))["fornecedores"][0]
        soma_d = sum(l["valor_debito"] for l in forn["lancamentos"])
        soma_c = sum(l["valor_credito"] for l in forn["lancamentos"])
        assert soma_d == forn["total_debito"] == Decimal("217.75")
        assert soma_c == forn["total_credito"] == Decimal("350.26")

    def test_saldo_negativo_vira_modulo_mais_tipo(self):
        """A planilha usa sinal; o resto do sistema usa módulo + C/D, como o PDF."""
        forn = parsear_planilha_razao(_planilha(_BLOCO_SIMPLES))["fornecedores"][0]
        assert forn["saldo_anterior"] == Decimal("1088.77")
        assert forn["saldo_anterior_tipo"] == "C"
        assert forn["lancamentos"][0]["saldo_apos_lancamento"] == Decimal("871.02")
        assert forn["lancamentos"][0]["saldo_tipo"] == "C"

    def test_extrai_nf_do_historico(self):
        forn = parsear_planilha_razao(_planilha(_BLOCO_SIMPLES))["fornecedores"][0]
        assert forn["lancamentos"][1]["numero_nf"] == "116722"

    def test_nenhum_lancamento_e_classificado_por_ia(self):
        forn = parsear_planilha_razao(_planilha(_BLOCO_SIMPLES))["fornecedores"][0]
        assert all(not l["classificado_por_ia"] for l in forn["lancamentos"])
        assert all(not l["classificacao_incerta"] for l in forn["lancamentos"])

    def test_conta_sem_movimento_aparece(self):
        """Coerente com o caminho do PDF: conta sem lançamento é resultado válido."""
        linhas = [
            ["Conta:", 1667, "2.1.3.01.0002", "", "", "CASSOL MATERIAIS LTDA"],
            _linha(historico="SALDO ANTERIOR", saldo=-7635.26),
            ["", "", "", "", "Total da conta:", "", "", "", 0, 0],
        ]
        r = parsear_planilha_razao(_planilha(linhas))

        assert r["total_fornecedores"] == 1
        forn = r["fornecedores"][0]
        assert forn["lancamentos"] == []
        assert forn["total_debito"] == Decimal("0")
        assert forn["saldo_anterior"] == Decimal("7635.26")

    def test_varios_fornecedores_em_sequencia(self):
        linhas = list(_BLOCO_SIMPLES) + [
            ["Conta:", 1685, "2.1.3.01.0010", "", "", "OUTRO FORNECEDOR LTDA"],
            _linha(data=datetime(2025, 5, 2), lote=77, historico="COMPRA",
                   cta=609, credito=100.00, saldo=-100.00),
            ["", "", "", "", "Total da conta:", "", "", "", 0, 100.00],
        ]
        r = parsear_planilha_razao(_planilha(linhas))

        assert r["total_fornecedores"] == 2
        assert [f["codigo_conta"] for f in r["fornecedores"]] == ["1670", "1685"]
        assert r["total_lancamentos"] == 3

    def test_divergencia_no_total_nao_derruba_o_parse(self):
        """Total declarado errado é sinalizado, não faz o arquivo falhar."""
        linhas = list(_BLOCO_SIMPLES)
        linhas[-1] = ["", "", "", "", "Total da conta:", "", "", "", 999.99, 350.26]
        r = parsear_planilha_razao(_planilha(linhas))

        forn = r["fornecedores"][0]
        assert len(forn["lancamentos"]) == 2
        assert forn["total_debito"] == Decimal("999.99")  # preserva o declarado

    def test_planilha_sem_cabecalho_reclama(self):
        wb = openpyxl.Workbook()
        wb.active.append(["qualquer", "coisa"])
        buf = BytesIO()
        wb.save(buf)

        with pytest.raises(ValueError, match="Débito"):
            parsear_planilha_razao(buf.getvalue())
