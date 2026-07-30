"""Testes unitários — reconstrução de linhas do parser ConcilPro.

Cobre o Formato 6: PDFs em que o histórico é desenhado várias vezes numa
baseline própria, a menos de 1 pt da baseline dos dados. O pdfplumber funde as
duas e, ordenando por x, entrelaça os caracteres — corrompendo o lançamento.

Os testes usam dicts de char sintéticos em vez de um PDF real: os PDFs de razão
disponíveis contêm CNPJ e razão social de clientes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from src.domain.concilpro.parser import (
    _colunas_do_header,
    _construir_runs,
    _corrigir_colunas_por_posicao,
    consolidar_fornecedores_duplicados,
    _escolher_melhor_extracao,
    _extrair_pagina_por_chars,
    _montar_linhas_de_runs,
    _pontuar_extracao,
    _recuperar_lancamentos_ocultos,
    _valores_por_coluna,
)

LARGURA_CHAR = 3.0


def _mk_chars(texto: str, x0: float, top: float) -> list[dict]:
    """Gera os chars de um trecho desenhado a partir de (x0, top)."""
    chars = []
    for i, ch in enumerate(texto):
        esquerda = x0 + i * LARGURA_CHAR
        chars.append({
            "text": ch,
            "x0": esquerda,
            "x1": esquerda + LARGURA_CHAR,
            "top": top,
            "bottom": top + 7.0,
        })
    return chars


class TestReconstrucaoLinhas:
    def test_copias_sobrepostas_colapsam_em_uma(self):
        """Formato 6: histórico repetido em baseline própria não deve entrelaçar."""
        historico = "VALOR A RECUPERAR"
        chars: list[dict] = []

        # 5 cópias do histórico, todas em top=112.18 e sobrepostas em x
        for x0 in (0.7, 40.0, 80.0, 120.0, 160.0):
            chars += _mk_chars(historico, x0, 112.18)

        # linha de dados, 0.96 pt abaixo — cada campo contíguo no stream
        chars += _mk_chars("28/02/2026", 0.0, 113.14)
        chars += _mk_chars("11192", 58.7, 113.14)
        chars += _mk_chars("2.758,20", 355.6, 113.14)

        linhas = _montar_linhas_de_runs(_construir_runs(chars))

        assert len(linhas) == 1, f"esperada 1 linha visual, veio {linhas}"
        linha = linhas[0]

        # o histórico aparece exatamente uma vez
        assert linha.count(historico) == 1, linha
        # nenhum campo se perdeu
        for campo in ("28/02/2026", "11192", "2.758,20"):
            assert campo in linha, f"{campo} ausente de: {linha}"
        # e nada foi entrelaçado
        assert _pontuar_extracao(linha)[1] == 0, linha

    def test_copias_sem_sobreposicao_sao_preservadas(self):
        """'Total da conta: X X' — débito e crédito iguais NÃO são artefato."""
        chars = _mk_chars("Total da conta:", 0.0, 200.0)
        chars += _mk_chars("40.679,43", 200.0, 200.0)
        chars += _mk_chars("40.679,43", 400.0, 200.0)

        linhas = _montar_linhas_de_runs(_construir_runs(chars))

        assert len(linhas) == 1
        assert linhas[0].count("40.679,43") == 2, (
            f"os dois totais devem sobreviver, veio: {linhas[0]}"
        )

    def test_linha_limpa_permanece_intacta(self):
        """PDF sem sobreposição não deve perder nem inventar conteúdo."""
        chars = _mk_chars("28/02/2026", 0.0, 50.0)
        chars += _mk_chars("11190", 60.0, 50.0)
        chars += _mk_chars("VALOR A RECOLHER", 120.0, 50.0)
        chars += _mk_chars("2.758,20", 300.0, 50.0)

        linhas = _montar_linhas_de_runs(_construir_runs(chars))

        assert len(linhas) == 1
        assert linhas[0].split() == ["28/02/2026", "11190", "VALOR", "A", "RECOLHER", "2.758,20"]

    def test_alinhamento_de_coluna_e_preservado(self):
        """
        É a posição x que distingue Débito de Crédito — colapsar em espaço
        simples faz a IA classificar todo lançamento como crédito.
        """
        largura = LARGURA_CHAR

        # header: Débito em x=389, Crédito em x=447 (posições reais do Razão)
        header = _mk_chars("Debito", 389.0, 40.0) + _mk_chars("Credito", 447.0, 40.0)
        # linha de crédito: valor sob a coluna Crédito
        credito = _mk_chars("31/03/2026", 0.0, 60.0) + _mk_chars("7.262,93", 449.5, 60.0)
        # linha de débito: valor sob a coluna Débito
        debito = _mk_chars("31/03/2026", 0.0, 80.0) + _mk_chars("7.262,93", 388.6, 80.0)

        l_header = _montar_linhas_de_runs(_construir_runs(header), largura)[0]
        l_credito = _montar_linhas_de_runs(_construir_runs(credito), largura)[0]
        l_debito = _montar_linhas_de_runs(_construir_runs(debito), largura)[0]

        col_debito = l_header.find("Debito")
        col_credito = l_header.find("Credito")

        assert abs(l_credito.find("7.262,93") - col_credito) <= 2, (
            f"valor de crédito fora da coluna:\n{l_header!r}\n{l_credito!r}"
        )
        assert abs(l_debito.find("7.262,93") - col_debito) <= 2, (
            f"valor de débito fora da coluna:\n{l_header!r}\n{l_debito!r}"
        )
        assert l_credito.find("7.262,93") != l_debito.find("7.262,93"), (
            "débito e crédito não podem cair na mesma coluna"
        )

    def test_dedup_nao_desloca_valores_das_colunas(self):
        """As cópias artefato não podem empurrar o valor real para fora da coluna."""
        chars: list[dict] = []
        # 5 cópias do histórico; a 4ª invade a faixa da coluna Débito
        for x0 in (0.7, 80.6, 146.1, 288.0, 416.2):
            chars += _mk_chars("VALOR A RECUPERAR DE IPI", x0, 112.18)
        chars += _mk_chars("31/03/2026", 0.0, 113.14)
        chars += _mk_chars("15606", 58.7, 113.14)
        chars += _mk_chars("7.262,93", 388.6, 113.14)

        linha = _montar_linhas_de_runs(_construir_runs(chars), LARGURA_CHAR)[0]

        assert linha.count("VALOR A RECUPERAR DE IPI") == 1, linha
        esperado = int(round(388.6 / LARGURA_CHAR))
        assert abs(linha.find("7.262,93") - esperado) <= 2, (
            f"valor deslocado: esperado ~{esperado}, veio {linha.find('7.262,93')}\n{linha!r}"
        )

    def test_baselines_distantes_viram_linhas_separadas(self):
        """Linhas visualmente distintas continuam separadas."""
        chars = _mk_chars("28/02/2026 primeira", 0.0, 100.0)
        chars += _mk_chars("31/03/2026 segunda", 0.0, 110.0)

        linhas = _montar_linhas_de_runs(_construir_runs(chars))

        assert len(linhas) == 2
        assert "primeira" in linhas[0]
        assert "segunda" in linhas[1]


class TestPontuarExtracao:
    def test_conta_linhas_de_lancamento(self):
        texto = "cabecalho\n28/02/2026 11190 COMPRA\n31/03/2026 11204 COMPRA\nrodape"
        lancamentos, corrompidos = _pontuar_extracao(texto)
        assert lancamentos == 2
        assert corrompidos == 0

    def test_detecta_token_entrelacado(self):
        limpo = "28/02/2026 11192 VALOR A RECUPERAR DE IPI"
        sujo = "2V8A/L0O2R/2 A02 R6ECUPERA1R1 1D9E2 IVPIA LDOOR"
        assert _pontuar_extracao(limpo)[1] == 0
        assert _pontuar_extracao(sujo)[1] > 0

    def test_valores_e_datas_nao_contam_como_sujos(self):
        texto = "30/06/2026 24.029,28 40.679,43C 2.1.4.01.0001"
        assert _pontuar_extracao(texto)[1] == 0


class _PaginaFake:
    """Stub de página do pdfplumber para testar a escolha de estratégia."""

    def __init__(self, texto_layout: str, texto_words: str, chars: list[dict]):
        self._layout = texto_layout
        self._words = texto_words
        self.chars = chars

    def extract_text(self, layout: bool = False) -> str:
        return self._layout

    def extract_words(self, **kwargs) -> list[dict]:
        # reaproveita os chars como "palavras" — suficiente para o stub
        return [dict(w, text=w["text"]) for w in self.chars] if self._words else []


class TestEscolherMelhorExtracao:
    def test_prefere_chars_quando_layout_esta_corrompido(self):
        """O caso real: layout tem mais texto, mas menos lançamentos legíveis."""
        chars: list[dict] = []
        for x0 in (0.7, 40.0, 80.0):
            chars += _mk_chars("VALOR A RECUPERAR", x0, 112.18)
        chars += _mk_chars("28/02/2026", 0.0, 113.14)
        chars += _mk_chars("11192", 58.7, 113.14)

        # layout corrompido e mais longo — o critério antigo (comprimento) o elegeria
        layout_corrompido = (
            "2V8A/L0O2R/2 A02 R6ECUPERA1R1 1D9E2 IVPIA LDOOR P AE RRÍEOCDUOPERAR "
            "VDAEL OIPRI AD OR EPCEURPÍOERDAOR DE IPI DO PERÍODO"
        )
        pagina = _PaginaFake(layout_corrompido, "", chars)

        escolhido = _escolher_melhor_extracao(pagina)

        assert escolhido == _extrair_pagina_por_chars(pagina)
        assert "28/02/2026" in escolhido
        assert _pontuar_extracao(escolhido)[1] == 0
        assert _pontuar_extracao(layout_corrompido)[1] > 0, (
            "o layout de referência precisa estar corrompido para o teste valer"
        )

    def test_mantem_layout_quando_ja_esta_limpo(self):
        """Empate preserva o comportamento histórico (sem regressão)."""
        chars = _mk_chars("28/02/2026 11190 COMPRA", 0.0, 50.0)
        layout_limpo = "28/02/2026 11190 COMPRA"
        pagina = _PaginaFake(layout_limpo, "", chars)

        assert _escolher_melhor_extracao(pagina) == layout_limpo


# Bloco real do Razão.pdf (AXEL / IPI A RECOLHER) com o alinhamento preservado.
# Colunas: Débito=105, Crédito=121, Saldo=137.
_HEADER = (
    "   Data         Lote  Histórico"
    "                                                        Cta.C.Part.       Débito"
    "          Crédito         Saldo-Exercício"
)
_LINHA_CREDITO = (
    "28/02/2026      11190 VALOR A RECOLHER DE IPI DO PERÍODO"
    "                                       425"
    "                      2.758,20                2.758,20C"
)
_LINHA_DEBITO = (
    "28/02/2026      11192 VALOR A RECUPERAR DE IPI DO PERÍODO"
    "                                       29       2.758,20"
    "                                    0,00"
)


def _lanc(data: str, lote: str, debito="0", credito="0", tipo="COMPRA") -> dict:
    return {
        "data_lancamento": datetime.strptime(data, "%d/%m/%Y"),
        "lote": lote,
        "valor_debito": Decimal(debito),
        "valor_credito": Decimal(credito),
        "saldo_apos_lancamento": Decimal("0"),
        "tipo_operacao": tipo,
        "historico": "x",
    }


class TestCorrecaoPorColuna:
    def test_localiza_colunas_do_header(self):
        colunas = _colunas_do_header([_HEADER])
        # colunas de valor são mapeadas pela borda DIREITA do rótulo
        assert colunas["valores"]["debito"] == _HEADER.find("Débito") + len("Débito")
        assert colunas["valores"]["credito"] == _HEADER.find("Crédito") + len("Crédito")
        assert colunas["valores"]["debito"] < colunas["valores"]["credito"]

    def test_tolerancia_se_adapta_a_colunas_estreitas(self):
        """Layout com Débito e Crédito a 7 chars não pode confundir as duas."""
        largo = "  Data  Lote  Histórico   Cta.C.Part.       Débito          Crédito    Saldo-Exercício"
        estreito = "  Data  Lote  Histórico   Cta.C.Part. Débito Crédito Saldo-Exercício"

        tol_largo = _colunas_do_header([largo])["tolerancia"]
        tol_estreito = _colunas_do_header([estreito])["tolerancia"]

        assert tol_estreito < tol_largo
        # a folga nunca pode alcançar a coluna vizinha
        cols = _colunas_do_header([estreito])["valores"]
        gap = cols["credito"] - cols["debito"]
        assert tol_estreito < gap

    def test_ignora_valor_da_coluna_saldo(self):
        """Saldo não pode ser somado como crédito."""
        colunas = _colunas_do_header([_HEADER])
        registro = _valores_por_coluna(_LINHA_CREDITO, colunas)
        # a linha tem 2.758,20 em Crédito e 2.758,20C em Saldo
        assert registro["credito"] == Decimal("2758.20")
        assert registro["debito"] == Decimal("0")

    def test_le_valor_na_coluna_credito(self):
        colunas = _colunas_do_header([_HEADER])
        registro = _valores_por_coluna(_LINHA_CREDITO, colunas)
        assert registro["lote"] == "11190"
        assert registro["credito"] == Decimal("2758.20")
        assert registro["debito"] == Decimal("0")

    def test_le_valor_na_coluna_debito(self):
        colunas = _colunas_do_header([_HEADER])
        registro = _valores_por_coluna(_LINHA_DEBITO, colunas)
        assert registro["lote"] == "11192"
        assert registro["debito"] == Decimal("2758.20")
        assert registro["credito"] == Decimal("0")

    def test_corrige_atribuicao_errada_da_ia(self):
        """O caso real: a IA zerou os dois valores da linha de débito."""
        linhas = [_HEADER, _LINHA_CREDITO, _LINHA_DEBITO]
        lancamentos = [
            _lanc("28/02/2026", "11190", credito="2758.20"),
            _lanc("28/02/2026", "11192"),          # IA não atribuiu nada
        ]

        _corrigir_colunas_por_posicao(lancamentos, linhas)

        assert lancamentos[0]["valor_credito"] == Decimal("2758.20")
        assert lancamentos[1]["valor_debito"] == Decimal("2758.20")
        assert lancamentos[1]["valor_credito"] == Decimal("0")
        assert lancamentos[1]["tipo_operacao"] == "PAGAMENTO"

    def test_palavra_chave_nao_prevalece_sobre_coluna(self):
        """'VALOR A RECUPERAR' casa com a regra de crédito, mas cai em Débito."""
        lancamentos = [_lanc("28/02/2026", "11192", credito="2758.20", tipo="COMPRA")]

        _corrigir_colunas_por_posicao(lancamentos, [_HEADER, _LINHA_DEBITO])

        assert lancamentos[0]["valor_debito"] == Decimal("2758.20")
        assert lancamentos[0]["valor_credito"] == Decimal("0")

    def test_sem_header_nao_altera_nada(self):
        """Formatos 1–5 não têm colunas — a IA continua decidindo."""
        original = _lanc("28/02/2026", "11192", credito="2758.20")
        lancamentos = [dict(original)]

        _corrigir_colunas_por_posicao(lancamentos, ["28/02/2026 11192 COMPRA 2.758,20"])

        assert lancamentos[0]["valor_credito"] == original["valor_credito"]
        assert lancamentos[0]["valor_debito"] == original["valor_debito"]


class TestConsolidacaoEntrePaginas:
    def test_descarta_lancamento_lido_duas_vezes(self):
        """
        Fornecedor que atravessa a quebra de página vira dois blocos: o parcial
        cai no fallback Vision (lê a página toda), a continuação é lida pelo
        texto. Os mesmos lançamentos chegam por dois caminhos.
        """
        base = _lanc("23/06/2025", "444", credito="249.99")
        base["saldo_apos_lancamento"] = Decimal("249.99")

        por_vision = dict(base, lote="365866", numero_nf="365866")
        por_texto = dict(base, numero_nf=None)

        consolidado = consolidar_fornecedores_duplicados([
            {"codigo_conta": "1495", "nome_fornecedor": "RODOPOSTO",
             "lancamentos": [por_vision], "total_credito": Decimal("249.99")},
            {"codigo_conta": "1495", "nome_fornecedor": "RODOPOSTO",
             "lancamentos": [por_texto], "total_credito": Decimal("249.99")},
        ])

        assert len(consolidado) == 1
        lancamentos = consolidado[0]["lancamentos"]
        assert len(lancamentos) == 1, "o lançamento duplicado deveria ter sido descartado"
        assert sum(l["valor_credito"] for l in lancamentos) == Decimal("249.99")
        # a versão preservada mantém a NF que só um dos caminhos trouxe
        assert lancamentos[0]["numero_nf"] == "365866"

    def test_preserva_lancamentos_legitimamente_iguais_em_datas_diferentes(self):
        """Mesmo valor em datas distintas, com saldos distintos, não é duplicata."""
        a = _lanc("19/05/2025", "376", credito="1021.90")
        a["saldo_apos_lancamento"] = Decimal("1021.90")
        b = _lanc("16/06/2025", "377", credito="1021.90")
        b["saldo_apos_lancamento"] = Decimal("2043.80")

        consolidado = consolidar_fornecedores_duplicados([
            {"codigo_conta": "1472", "nome_fornecedor": "BOIKO", "lancamentos": [a]},
            {"codigo_conta": "1472", "nome_fornecedor": "BOIKO", "lancamentos": [b]},
        ])

        assert len(consolidado[0]["lancamentos"]) == 2

    def test_descarta_duplicata_com_data_divergente(self):
        """
        Caso BOIKO: os dois caminhos leram o mesmo lançamento com datas
        diferentes. Mesmo lote, mesmo valor e MESMO saldo resultante —
        aritmeticamente impossível para dois lançamentos distintos.
        """
        a = _lanc("01/08/2025", "12530", debito="807.30", tipo="PAGAMENTO")
        a["saldo_apos_lancamento"] = Decimal("1614.60")
        b = _lanc("21/08/2025", "12530", debito="807.30", tipo="PAGAMENTO")
        b["saldo_apos_lancamento"] = Decimal("1614.60")

        consolidado = consolidar_fornecedores_duplicados([
            {"codigo_conta": "1472", "nome_fornecedor": "BOIKO", "lancamentos": [a, b]},
        ])

        assert len(consolidado[0]["lancamentos"]) == 1

    def test_deduplica_dentro_de_um_unico_bloco(self):
        """A releitura texto+Vision pode duplicar dentro do próprio fornecedor."""
        a = _lanc("23/06/2025", "444", credito="249.99")
        a["saldo_apos_lancamento"] = Decimal("249.99")
        b = dict(a, numero_nf="365866")

        consolidado = consolidar_fornecedores_duplicados([
            {"codigo_conta": "1495", "nome_fornecedor": "RODOPOSTO", "lancamentos": [a, b]},
        ])

        assert len(consolidado[0]["lancamentos"]) == 1
        assert consolidado[0]["lancamentos"][0]["numero_nf"] == "365866"


class TestRecuperacaoOculta:
    def test_nao_fabrica_quando_totais_fecham(self):
        """Se o PDF declara totais que batem, nada está oculto."""
        lancamentos = [
            _lanc("28/02/2026", "11190", credito="2758.20"),
            _lanc("28/02/2026", "11192", debito="2758.20"),
        ]
        lancamentos[0]["saldo_apos_lancamento"] = Decimal("2758.20")
        lancamentos[1]["saldo_apos_lancamento"] = Decimal("0")

        resultado = _recuperar_lancamentos_ocultos(
            lancamentos, [_HEADER],
            total_debito_declarado=Decimal("2758.20"),
            total_credito_declarado=Decimal("2758.20"),
        )

        assert len(resultado) == 2
        assert not any(l.get("sintetico") for l in resultado)
