"""Casamento de contraparte pelo nome no histórico do extrato."""

from src.domain.neo.contraparte_por_nome import (
    CandidataPorNome,
    casar_por_nome,
    nucleo_do_nome,
    nucleo_utilizavel,
)


def _c(id_: str, nome: str) -> CandidataPorNome:
    return CandidataPorNome(contraparte_id=id_, nucleo=nucleo_do_nome(nome))


# ── Núcleo do nome ───────────────────────────────────────────────────────────


def test_remove_sufixo_societario_do_fim():
    """O extrato quase nunca traz 'LTDA'; mantê-lo só produziria falso negativo."""
    assert nucleo_do_nome("Forcecar Automotive Ltda.") == "forcecar automotive"
    assert nucleo_do_nome("METALURGICA PEDRON ME") == "metalurgica pedron"
    assert nucleo_do_nome("Zahra Perfumes S/A") == "zahra perfumes"


def test_nao_remove_sufixo_do_meio_nem_do_comeco():
    """'Cia Brasileira' perderia a primeira palavra se a limpeza fosse global."""
    assert nucleo_do_nome("Cia Brasileira de Alimentos") == "cia brasileira de alimentos"


def test_normaliza_acento_e_pontuacao():
    assert nucleo_do_nome("Construções Águia S.A.") == "construcoes aguia"


# ── Guarda de tamanho ────────────────────────────────────────────────────────


def test_nucleo_de_um_token_curto_nao_serve():
    """'ABC' casaria dentro de dezenas de históricos sem relação."""
    assert nucleo_utilizavel("abc") is False
    assert nucleo_utilizavel("unimed") is False       # 6 < 8, um token só
    assert nucleo_utilizavel("metalurgica") is True   # token longo o bastante


def test_dois_tokens_ja_dao_especificidade():
    assert nucleo_utilizavel("unimed curitiba") is True


def test_nucleo_vazio_nao_serve():
    assert nucleo_utilizavel("") is False
    assert nucleo_do_nome(None) == ""


# ── Casamento ────────────────────────────────────────────────────────────────


def test_casa_fornecedor_pelo_nome_no_historico():
    """O caso do relatório: cadastrado em Contrapartes, nome no extrato."""
    candidatas = [_c("1", "Unimed Curitiba"), _c("2", "Sanepar Saneamento")]

    achada, motivo = casar_por_nome(
        "PAGAMENTO PIX 12345678000199 UNIMED CURITIBA", candidatas
    )

    assert achada is not None
    assert achada.contraparte_id == "1"
    assert motivo is None


def test_casa_mesmo_com_sufixo_no_cadastro_e_nao_no_extrato():
    candidatas = [_c("1", "Forcecar Automotive Ltda.")]

    achada, _ = casar_por_nome("PAGAMENTO PIX 05772675000172 FORCECAR AUTOMOTIVE", candidatas)

    assert achada is not None


def test_historico_sem_o_nome_nao_casa():
    candidatas = [_c("1", "Unimed Curitiba")]

    achada, motivo = casar_por_nome("TARIFA COM R LIQUIDACAO COB000001", candidatas)

    assert achada is None
    assert motivo is None       # não casou não é conflito, é silêncio


def test_empate_entre_cadastros_recusa_e_explica():
    """Escolher um seria adivinhar — a chance de acertar é metade."""
    candidatas = [_c("1", "Transportes Silva"), _c("2", "Transportes Silva Junior")]

    achada, motivo = casar_por_nome("TED 999 TRANSPORTES SILVA JUNIOR", candidatas)

    assert achada is None
    assert "Mais de uma contraparte" in motivo


def test_nomes_do_mesmo_cadastro_nao_sao_empate():
    """Razão social e nome fantasia parecidos apontam para a mesma empresa."""
    candidatas = [
        CandidataPorNome(contraparte_id="1", nucleo=nucleo_do_nome("Padaria Estrela Ltda")),
        CandidataPorNome(contraparte_id="1", nucleo=nucleo_do_nome("Padaria Estrela")),
    ]

    achada, motivo = casar_por_nome("COMPRAS NACIONAIS PADARIA ESTRELA", candidatas)

    assert achada is not None
    assert achada.contraparte_id == "1"
    assert motivo is None


def test_entre_nucleos_do_mesmo_cadastro_vence_o_mais_especifico():
    candidatas = [
        CandidataPorNome(contraparte_id="1", nucleo="padaria estrela"),
        CandidataPorNome(contraparte_id="1", nucleo="padaria estrela do sul"),
    ]

    achada, _ = casar_por_nome("PIX PADARIA ESTRELA DO SUL", candidatas)

    assert achada.nucleo == "padaria estrela do sul"


def test_cadastro_curto_demais_e_ignorado_e_nao_causa_empate():
    """Guarda de tamanho age antes do empate — senão um nome ruim travaria tudo."""
    candidatas = [_c("1", "ABC"), _c("2", "Metalurgica Pedron")]

    achada, motivo = casar_por_nome("PAGAMENTO METALURGICA PEDRON ABC", candidatas)

    assert achada is not None
    assert achada.contraparte_id == "2"
    assert motivo is None
