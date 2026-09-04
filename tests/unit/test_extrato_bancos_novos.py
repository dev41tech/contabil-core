"""Viacredi e Daycoval — dois adaptadores novos.

Os dados reproduzem a estrutura real, com razão social e valores fictícios.
Nenhum dado de cliente entra no repositório.

Cada teste trava um defeito que **falharia em silêncio** — sinal trocado ou
lançamento a menos, não erro de execução:

- **Viacredi:** a conta fecha o mês no vermelho, e o saldo negativo é impresso
  com `-` como qualquer outro valor. Um padrão que exigisse dígito na coluna do
  saldo recusaria o extrato a partir do dia em que a conta vira — o mesmo
  defeito que já custou o layout "Gerenciador" da Caixa.
- **Viacredi:** a linha `TOTAL` traz três valores e casaria com o padrão de
  dados se a data não fosse obrigatória. Somá-la duplicaria o mês.
- **Daycoval:** o sinal vem de QUAL coluna recebeu o número; a outra é um traço.
  Decidir pelo texto erraria — `AMORT. DE CONTRATO` é débito e `TED-CREDITO` é
  crédito, e os dois trazem a palavra "crédito" ou não por acaso.
- **Daycoval:** a descrição longa é quebrada ACIMA e ABAIXO da linha de dados,
  que fica com a coluna `Lançamento` vazia. Colar fora de ordem devolve o fim
  do nome antes do começo.
- **Daycoval:** o traço solto DENTRO da descrição (`TED-CREDITO - 341 ...`) não
  pode ser lido como coluna vazia.
"""

from datetime import date
from decimal import Decimal

from src.domain.extrato import bancos
from src.domain.extrato.bancos import daycoval, safra, unicred, viacredi

# ─────────────────────────────────────────────────────────────────── Viacredi

_VIACREDI = """\
Emitido em 04/05/2026 às 14:09:35
EXTRATO
Período 01/04/2026 a 30/04/2026
Nome: EXEMPLO TRANSPORTES LTDA
Cooperativa: VIACREDI | Banco: 085 | Agência: 0101-5 | Conta: 17347297
DATA DESCRIÇÃO DOCUMENTO CRÉDITO (R$) DÉBITO (R$) SALDO (R$)
SALDO ANTERIOR 1.000,00
01/04/2026 CREDITO PIX - ALFA SERVICOS ADMINISTRATIVOS 806790.512 500,00 1.500,00
01/04/2026 IOF S/ C-C 897.753 -59,52 1.440,48
02/04/2026 PG.P/INTERNET - BETA COMERCIO 10119.785 -2.000,00 -559,52
03/04/2026 SOBRAS REF. TARIFAS 80052.179 21,24 -538,28
TOTAL 521,24 -2.059,52 -538,28
Os dados acima têm como base 04/05/2026 às 14:09 e estão sujeitos a alterações.
SAC - 0800 000 0000 | Atendimento de Segunda a Sexta
""".splitlines()


def test_viacredi_reconhece_pela_cooperativa_e_codigo_do_banco():
    assert viacredi.reconhece(_VIACREDI) is True
    assert bancos.por_conteudo(_VIACREDI) is viacredi


def test_viacredi_le_todos_os_lancamentos_e_o_saldo_de_abertura():
    (bloco,) = viacredi.extrair(_VIACREDI, 2026)

    assert len(bloco.transacoes) == 4
    assert bloco.saldo_anterior == Decimal("1000.00")


def test_viacredi_o_sinal_vem_do_proprio_valor():
    (bloco,) = viacredi.extrair(_VIACREDI, 2026)
    valores = [t.valor for t in bloco.transacoes]

    assert valores == [
        Decimal("500.00"),
        Decimal("-59.52"),
        Decimal("-2000.00"),
        Decimal("21.24"),
    ]


def test_viacredi_saldo_negativo_nao_derruba_a_leitura():
    """A conta vira o mês no vermelho, e o extrato continua legível.

    Sem isto, todo lançamento a partir do dia em que a conta entra no limite
    seria descartado — e o extrato pareceria simplesmente mais curto.
    """
    (bloco,) = viacredi.extrair(_VIACREDI, 2026)

    saldos = [t.saldo_apos for t in bloco.transacoes]
    assert saldos[-2:] == [Decimal("-559.52"), Decimal("-538.28")]


def test_viacredi_a_cadeia_de_saldos_fecha():
    (bloco,) = viacredi.extrair(_VIACREDI, 2026)

    esperado = bloco.saldo_anterior + sum(t.valor for t in bloco.transacoes)
    assert esperado == bloco.transacoes[-1].saldo_apos


def test_viacredi_a_linha_total_nao_vira_lancamento():
    """`TOTAL 521,24 -2.059,52 -538,28` tem a forma de dados e não é dado."""
    (bloco,) = viacredi.extrair(_VIACREDI, 2026)

    assert all("TOTAL" not in t.historico for t in bloco.transacoes)
    assert sum(t.valor for t in bloco.transacoes) == Decimal("-1538.28")


# ─────────────────────────────────────────────────────────────────── Daycoval

_DAYCOVAL = """\
Página 1 de 2
Extrato Detalhado
Titular
EXEMPLO EXPRESS LTDA
Agência
00019
Conta
0015124870
Período consultado
01/07/2026 à 31/07/2026
Data Nº Docto Lançamento Débito (R$) Crédito (R$) Saldo (R$)
SALDO ANTERIOR 100,00
02/07/2026 9235551 TARIFA DE MANUTENCAO DE C/C 40,00 -
SALDO EM 02/07/2026 60,00
RECEBIMENTO PIX - Cp: 90400888-3689-130363264-SHPX
09/07/2026 8292562 - 1.000,00
LOGISTICA LTDA
09/07/2026 9000000 TARIFA PIX 2,65 -
SALDO EM 09/07/2026 1.057,35
20/07/2026 2120586 TED-CREDITO - 341 7285 989245 EXEMPLO LTDA - 500,00
SALDO EM 20/07/2026 1.557,35
Impressão realizada em 31/07/2026 10:01:51
Central de Atendimento Dayconnect - 11 0000-0000
""".splitlines()


def test_daycoval_reconhece_pelo_titulo_mais_o_saldo_do_dia():
    assert daycoval.reconhece(_DAYCOVAL) is True
    assert bancos.por_conteudo(_DAYCOVAL) is daycoval


def test_daycoval_o_sinal_vem_da_coluna_nao_do_texto():
    """A coluna vazia é um traço; o lado em que o número caiu define o sinal."""
    (bloco,) = daycoval.extrair(_DAYCOVAL, 2026)
    valores = [t.valor for t in bloco.transacoes]

    assert valores == [
        Decimal("-40.00"),    # débito: número na 1ª coluna, traço na 2ª
        Decimal("1000.00"),   # crédito: traço na 1ª, número na 2ª
        Decimal("-2.65"),
        Decimal("500.00"),
    ]


def test_daycoval_descricao_quebrada_e_colada_na_ordem_de_leitura():
    """O trecho de CIMA vem antes do de baixo.

    Invertido, o histórico sai com o fim do nome na frente do começo, e
    nenhuma busca por razão social o encontra.
    """
    (bloco,) = daycoval.extrair(_DAYCOVAL, 2026)
    recebimento = bloco.transacoes[1]

    assert recebimento.historico.startswith("RECEBIMENTO PIX")
    assert recebimento.historico.endswith("LOGISTICA LTDA")


def test_daycoval_traco_dentro_da_descricao_nao_e_coluna_vazia():
    """`TED-CREDITO - 341 7285 989245 EXEMPLO LTDA - 500,00` é UM crédito."""
    (bloco,) = daycoval.extrair(_DAYCOVAL, 2026)
    ted = bloco.transacoes[-1]

    assert ted.valor == Decimal("500.00")
    assert "TED-CREDITO" in ted.historico


def test_daycoval_saldo_do_dia_ancora_o_ultimo_lancamento_dele():
    (bloco,) = daycoval.extrair(_DAYCOVAL, 2026)

    # 02/07 tem um lançamento só: o saldo do dia é dele.
    assert bloco.transacoes[0].saldo_apos == Decimal("60.00")
    # 09/07 tem dois: o saldo fecha no SEGUNDO, não no primeiro.
    assert bloco.transacoes[1].saldo_apos is None
    assert bloco.transacoes[2].saldo_apos == Decimal("1057.35")


def test_daycoval_a_cadeia_de_saldos_fecha():
    (bloco,) = daycoval.extrair(_DAYCOVAL, 2026)

    esperado = bloco.saldo_anterior + sum(t.valor for t in bloco.transacoes)
    assert esperado == bloco.transacoes[-1].saldo_apos


def test_daycoval_saldo_do_dia_negativo_e_lido():
    """Conta com limite fecha o dia no vermelho, e isso não é erro de leitura."""
    linhas = [
        ln.replace("SALDO EM 02/07/2026 60,00", "SALDO EM 02/07/2026 -40,00")
        for ln in _DAYCOVAL
    ]
    linhas = [ln.replace("SALDO ANTERIOR 100,00", "SALDO ANTERIOR 0,00") for ln in linhas]

    (bloco,) = daycoval.extrair(linhas, 2026)

    assert bloco.transacoes[0].saldo_apos == Decimal("-40.00")


# ────────────────────────────────────────────────────────────────────── Safra

_SAFRA = """\
Banco Safra S/A Página 1 de 1
CNPJ: 58.160.789/0001-28
04/08/2026 13:36
Extrato de Movimentação EXEMPLO TRANSPORTES LTDA
CNPJ: 000000000 | AG: 0038 | CONTA: 00671265-4
Período de 29/07/2026 a 04/08/2026
Saldo + Limite Disponível Saldo Saldo Bloqueado Limite Cheque Empresarial
R$ 1.000,00 R$ 1.000,00 R$ 0,00 R$ 0,00
LANÇAMENTOS REALIZADOS
Data Lançamento Complemento Nº Documento Valor (R$)
29/07 RESGATE FUNDO INVEST SAFRA SOBERANO 269062182 500,00
29/07 PIX ENVIADO ALFA INCORPORADORA DE 268724779 -300,00
BENS 46515935000101
29/07 SALDO TOTAL 1.200,00
03/08 OUTROS CUSTOS BMF 271490805 -0,80
03/08 SALDO TOTAL 1.199,20
CENTRAL DE SUPORTE A PESSOA JURÍDICA SAC E DEFICIENTES
""".splitlines()


def test_safra_reconhece_pelo_nome_do_banco():
    assert safra.reconhece(_SAFRA) is True
    assert bancos.por_conteudo(_SAFRA) is safra


def test_safra_saldo_total_nao_vira_lancamento():
    """`29/07 SALDO TOTAL 1.200,00` tem a forma exata de um lançamento.

    Somá-la injetaria o saldo dentro do movimento — a mesma família do defeito
    que pôs saldo no lugar de valor neste módulo em agosto.
    """
    (bloco,) = safra.extrair(_SAFRA, 2026)

    assert len(bloco.transacoes) == 3
    assert all("SALDO" not in t.historico.upper() for t in bloco.transacoes)


def test_safra_a_data_sem_ano_usa_a_referencia():
    """`29/07` não traz o ano; ele vem do período do cabeçalho."""
    (bloco,) = safra.extrair(_SAFRA, 2026)

    assert bloco.transacoes[0].data == date(2026, 7, 29)


def test_safra_descricao_transborda_para_a_linha_de_baixo():
    (bloco,) = safra.extrair(_SAFRA, 2026)
    pix = bloco.transacoes[1]

    assert pix.historico.startswith("PIX ENVIADO ALFA INCORPORADORA DE")
    assert pix.historico.endswith("BENS 46515935000101")


def test_safra_saldo_do_dia_ancora_o_ultimo_lancamento_dele():
    (bloco,) = safra.extrair(_SAFRA, 2026)

    assert bloco.transacoes[0].saldo_apos is None
    assert bloco.transacoes[1].saldo_apos == Decimal("1200.00")
    assert bloco.transacoes[2].saldo_apos == Decimal("1199.20")


# ──────────────────────────────────────────────────────────────────── Unicred

_UNICRED = """\
01/12/2025 09:37:52
Extrato
EXEMPLO MANUTENCAO E INSTAL - **.***.
Período de 01/11/2025 a 30/11/2025
Coop: 582 - AG: 1740 - Conta: 182443
Solicitado por EXEMPLO - ***.963.809-**
Saldo atual -R$ 500,00
Saldo em 31/10/2025: R$ 1.000,00 Total Disponível R$ 1,03
Limite de cheque especial R$ 10.000,00
Data Lançamentos Valor (R$) Saldo (R$)
03/11/2025 PJ CONTA PJ 4 ( Doc.: 0 ) - R$ 100,00 -R$ 100,00
DEBITO TRANSFERENCIA PIX ( Doc.: DEB PIX /
03/11/2025 - R$ 200,00 -R$ 300,00
ALFA IND E COM LTDA )
13/11/2025 CREDITO RECEBIMENTO ( Doc.: CRED ) R$ 50,00 -R$ 250,00
CENTRAL DE RELACIONAMENTO: 0800 000 0000
""".splitlines()


def test_unicred_reconhece_pela_forma_abreviada_da_cooperativa():
    assert unicred.reconhece(_UNICRED) is True
    assert bancos.por_conteudo(_UNICRED) is unicred


def test_unicred_o_sinal_vem_antes_do_cifrao():
    (bloco,) = unicred.extrair(_UNICRED, 2025)
    valores = [t.valor for t in bloco.transacoes]

    assert valores == [Decimal("-100.00"), Decimal("-200.00"), Decimal("50.00")]


def test_unicred_saldo_negativo_e_lido_com_o_sinal_colado_no_cifrao():
    """Débito escreve `- R$` com espaço; o saldo escreve `-R$` sem. Os dois valem."""
    (bloco,) = unicred.extrair(_UNICRED, 2025)

    assert [t.saldo_apos for t in bloco.transacoes] == [
        Decimal("-100.00"),
        Decimal("-300.00"),
        Decimal("-250.00"),
    ]


def test_unicred_descricao_envolve_a_linha_de_dados():
    """O trecho de cima abre o parêntese e o de baixo o fecha — a ordem importa."""
    (bloco,) = unicred.extrair(_UNICRED, 2025)
    pix = bloco.transacoes[1]

    assert pix.historico == "DEBITO TRANSFERENCIA PIX ( Doc.: DEB PIX / ALFA IND E COM LTDA )"


def test_unicred_nao_ancora_no_saldo_do_cabecalho():
    """`Saldo em ...` não carrega sinal, e ancorar nele desloca o mês inteiro.

    Medido em dois extratos reais com este mesmo formato: num deles a cadeia só
    fecha com o valor positivo, no outro só com o negativo, e nada no texto
    separa os dois casos. A coluna `Saldo (R$)` de cada linha é a única âncora
    verdadeira aqui.
    """
    (bloco,) = unicred.extrair(_UNICRED, 2025)

    assert bloco.saldo_anterior is None
    assert all(t.saldo_apos is not None for t in bloco.transacoes)
