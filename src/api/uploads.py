"""Leitura de arquivos enviados por upload, com limite de tamanho."""

from __future__ import annotations

from fastapi import UploadFile

from src.core.config import get_settings
from src.core.errors import PayloadTooLargeError

_CHUNK_BYTES = 1024 * 1024


async def ler_upload_limitado(file: UploadFile, max_bytes: int | None = None) -> bytes:
    """Lê o upload inteiro, abortando assim que ultrapassar o limite.

    O `LimiteUploadMiddleware` já corta pelo `Content-Length` declarado. Esta
    contagem cobre o request que não declara tamanho (`Transfer-Encoding:
    chunked`): sem ela um corpo sem limite entraria inteiro na memória do worker,
    que é como todos os parsers do sistema consomem o arquivo.

    Levanta `PayloadTooLargeError` (HTTP 413).
    """
    limite = max_bytes if max_bytes is not None else get_settings().max_upload_bytes

    pedacos: list[bytes] = []
    total = 0
    while pedaco := await file.read(_CHUNK_BYTES):
        total += len(pedaco)
        if total > limite:
            raise PayloadTooLargeError(
                message=f"Arquivo maior que o limite de {limite // (1024 * 1024)} MB.",
                details={"limite_bytes": limite},
            )
        pedacos.append(pedaco)

    return b"".join(pedacos)
