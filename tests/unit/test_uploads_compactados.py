"""Limites internos de ZIP e XLSX contra expansão descontrolada."""

import io
import zipfile

import pytest

from src.api.v1.plano_contas import _validar_xlsx_compactado
from src.core.errors import ValidationError
from src.domain.notas.service import _validar_zip


def test_xlsx_com_razao_de_compressao_extrema_e_rejeitado():
    conteudo = io.BytesIO()
    with zipfile.ZipFile(conteudo, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"0" * (1024 * 1024))

    with pytest.raises(ValidationError, match="compressão"):
        _validar_xlsx_compactado(conteudo.getvalue())


def test_zip_de_notas_com_xml_grande_e_rejeitado():
    info = zipfile.ZipInfo("nota.xml")
    info.file_size = 11 * 1024 * 1024
    info.compress_size = info.file_size

    erro = _validar_zip([info])

    assert erro is not None
    assert "10 MiB" in erro
