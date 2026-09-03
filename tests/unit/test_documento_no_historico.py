"""Testes unitários — CPF/CNPJ impresso na linha do extrato.

As linhas vêm de testes reais do contador na SINCOPEÇAS (26/08/2026), onde o
fornecedor estava cadastrado com o CNPJ exato e a transação ficava pendente.
"""

from __future__ import annotations

import pytest

from src.domain.neo.documento_no_historico import documentos_no_historico


@pytest.mark.parametrize(
    "historico,esperado",
    [
        # Os dois casos que o contador testou e não classificaram.
        (
            "PAGAMENTO PIX 09033833000123 PERFORMANCE ENGENHA PIX_DEB",
            ["09033833000123"],
        ),
        (
            "LIQUIDACAO BOLETO SICREDI 31052957000105 041 CON 252080163",
            ["31052957000105"],
        ),
        # Convênio: o CNPJ é o do convenente, e vem no meio de valores.
        (
            "20/02/2026 DEBITO CONVENIOS 76417005000186 PMCURIT-G -1.788,13 -70.254,20",
            ["76417005000186"],
        ),
        # Forma pontuada.
        ("PIX 09.033.833/0001-23 PERFORMANCE", ["09033833000123"]),
        ("TED 123.456.789-09 JOAO", ["12345678909"]),
        # O mesmo documento duas vezes não é ambiguidade.
        (
            "PIX 09.033.833/0001-23 PERFORMANCE 09033833000123",
            ["09033833000123"],
        ),
        # Lixo numérico que NÃO pode virar documento.
        ("TARIFA BAIXA DE TITULOS COB000004", []),
        ("LIQUIDACAO BOLETO 252080163 SICREDI 251009131", []),
        # Código de barras de 24 dígitos: um pedaço dele não é CNPJ.
        ("BOLETO 123456789012345678901234 NOSSO NUMERO", []),
        ("", []),
    ],
)
def test_documentos_no_historico(historico: str, esperado: list[str]):
    assert documentos_no_historico(historico) == esperado


def test_dois_documentos_na_mesma_linha_saem_na_ordem_de_leitura():
    """O caso do boleto Sicredi: o CNPJ do banco e o do cedente na mesma linha.

    Quem chama precisa dos dois para perceber a ambiguidade — devolver só o
    primeiro esconderia o conflito e classificaria pelo banco.
    """
    historico = "LIQUIDACAO BOLETO SICREDI 07070495000174 CED 31052957000105"
    assert documentos_no_historico(historico) == [
        "07070495000174",
        "31052957000105",
    ]


def test_historico_none_nao_quebra():
    assert documentos_no_historico(None) == []


# ────────────────────────────── Cada banco pontua o CNPJ de um jeito diferente

@pytest.mark.parametrize(
    "historico,esperado",
    [
        # O caso que motivou o afrouxamento: o Sicoob escreve ESPAÇO no lugar da
        # barra. Nos seis extratos de jan–jun/2026 de uma conta só, eram 661
        # CNPJs que o extrator achava zero.
        (
            "PIX EMITIDO OUTRA IF Pagamento Pix 81.450.538 0001-08",
            ["81450538000108"],
        ),
        # Ponto no lugar da barra.
        ("TED 09.033.833.0001-23 CLIENTE", ["09033833000123"]),
        # Sem separador entre a raiz e a ordem.
        ("DOC 09.033.8330001-23", ["09033833000123"]),
        # Sem o traço do dígito verificador.
        ("PIX 09.033.833/000123 FORNECEDOR", ["09033833000123"]),
        # CPF sem o traço.
        ("PIX 123.456.78901 JOAO", ["12345678901"]),
        # CPF com espaço no lugar do traço.
        ("SAQUE NA AGENCIA NOME: PAULO CPF: 032.465.659 96", ["03246565996"]),
    ],
)
def test_formatos_que_os_bancos_realmente_imprimem(historico, esperado):
    assert documentos_no_historico(historico) == esperado


@pytest.mark.parametrize(
    "historico",
    [
        # SEM os dois pontos obrigatórios, um par de números soltos separados por
        # espaço não vira candidato. É o que impede o afrouxamento de virar
        # frouxidão — sem esta guarda, qualquer linha com cinco grupos de
        # números viraria um CNPJ inventado.
        "DOC 12 345 678 9012 34 REF",
        "TRANSF 09 033 833 0001 23",
        # Valor monetário brasileiro: a vírgula decimal quebra o padrão.
        "TARIFA 12.345.678,90 COBRADA",
        # Número de processo judicial, 20 dígitos. Um pedaço de 14 não é CNPJ —
        # é a guarda de corrida MÁXIMA, e ela sobrevive ao afrouxamento.
        "CREDITO LIBERACAO JUDICIAL 50628121920254047000",
    ],
)
def test_afrouxar_o_separador_nao_inventa_documento(historico):
    assert documentos_no_historico(historico) == []


def test_cpf_mascarado_pelo_banco_nao_resolve():
    """PIX para pessoa física vem com o CPF mascarado, e não há o que recuperar.

    São 442 das 1.103 linhas com documento aparente nos extratos do Sicoob. Não
    é defeito a corrigir: sem os dígitos, resolver depende do nome, que é
    evidência mais fraca, ou da classificação manual.
    """
    assert documentos_no_historico("Pagamento Pix ***.674.379-**") == []


def test_cnpj_pontuado_nao_gera_um_cpf_fantasma_dentro_dele():
    """Os grupos têm tamanho fixo, então nenhum trecho do CNPJ casa como CPF.

    Se casasse, a linha traria DOIS documentos e viraria recusa por ambiguidade
    — a transação deixaria de classificar por causa da própria correção.
    """
    for historico in (
        "PIX 81.450.538 0001-08 FORNECEDOR",
        "PIX 09.033.833.0001-23 FORNECEDOR",
        "PIX 09.033.833/0001-23 FORNECEDOR",
    ):
        assert len(documentos_no_historico(historico)) == 1


@pytest.mark.parametrize(
    "como_foi_cadastrado",
    [
        "52.540.787/0001-88",   # como o contador digita
        "52540787000188",       # como vem de um arquivo
        "52.540.787 0001-88",   # como o Sicoob imprime
        "52.540.787.0001-88",
    ],
)
@pytest.mark.parametrize(
    "como_o_banco_imprimiu",
    [
        "PIX 52.540.787/0001-88 FORNECEDOR",
        "PIX 52540787000188 FORNECEDOR",
        "PIX EMITIDO OUTRA IF Pagamento Pix 52.540.787 0001-88",
        "TED 52.540.787.0001-88 FORNECEDOR",
    ],
)
def test_cadastro_e_extrato_se_encontram_em_qualquer_formatacao(
    como_foi_cadastrado, como_o_banco_imprimiu
):
    """O requisito do escritório, nas duas direções, como um teste só.

    A ponta do CADASTRO é resolvida no schema: `ContraparteCreate` normaliza
    para dígitos antes de gravar, e a coluna é `String(14)` — CNPJ pontuado tem
    18 caracteres e nem caberia. A ponta do EXTRATO é este módulo. As duas
    precisam chegar na mesma string, senão o cadastro existe e a transação fica
    pendente para sempre, que era o sintoma.
    """
    from src.schemas.contrapartes import ContraparteCreate

    cadastrado = ContraparteCreate(
        tipo="fornecedor",
        documento=como_foi_cadastrado,
        razao_social="Fornecedor Exemplo Ltda",
        conta_contabil_id="00000000-0000-0000-0000-000000000001",
    ).documento

    assert documentos_no_historico(como_o_banco_imprimiu) == [cadastrado]


# ── Terceira volta: o que separa documento de fileira de números é QUAL separador

@pytest.mark.parametrize(
    "historico,esperado",
    [
        # Os três formatos que o escritório relatou em 02/09, depois de a
        # segunda versão ainda deixar um de fora.
        ("PIX 41.250.201 0001-24 FORNECEDOR", ["41250201000124"]),
        ("PIX 41250201 0001-24 FORNECEDOR", ["41250201000124"]),
        ("PIX 41.250.201-0001-24 FORNECEDOR", ["41250201000124"]),
        # O que já passava, e não pode regredir.
        ("PIX 41.250.201/0001-24 FORNECEDOR", ["41250201000124"]),
        ("PIX 41250201000124 FORNECEDOR", ["41250201000124"]),
        ("PIX 41.250.201.0001-24 FORNECEDOR", ["41250201000124"]),
        ("PIX 41250201/0001-24 FORNECEDOR", ["41250201000124"]),
        # CPF com a raiz sem pontuação.
        ("TED 123456789-01 JOAO", ["12345678901"]),
    ],
)
def test_basta_um_separador_forte_em_qualquer_posicao(historico, esperado):
    """Ponto, barra ou traço marcam documento formatado — em qualquer posição.

    As duas primeiras versões deste módulo descreveram os formatos que tinham
    sido vistos (primeiro a barra, depois os dois pontos) em vez da propriedade
    que os distingue, e cada uma deixou de fora o formato seguinte que apareceu.
    """
    assert documentos_no_historico(historico) == esperado


@pytest.mark.parametrize(
    "historico",
    [
        # Espaço é o separador que aparece entre dois números QUAISQUER, e por
        # isso sozinho não basta. Sem esta guarda o padrão casaria qualquer
        # fileira de números da linha do extrato.
        "DOC 12 345 678 9012 34 REF",
        "TRANSF 09 033 833 0001 23",
        "SALDO 12345678 9012 34 REF",
        # Valor monetário: a vírgula decimal quebra o padrão.
        "TARIFA 12.345.678,90 COBRADA",
        # Data com pontos seguida de número: os grupos não fecham em 2-3-3-4-2.
        "EM 01.02.2026 VALOR 1234-56 REF",
        # Corrida máxima: um pedaço de 14 dígitos de um número de 20 não é CNPJ.
        "CREDITO LIBERACAO JUDICIAL 50628121920254047000",
    ],
)
def test_espaco_sozinho_nao_faz_documento(historico):
    assert documentos_no_historico(historico) == []
