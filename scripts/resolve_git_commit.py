"""Identifica a versão do código que virou a imagem, para `GET /api/health`.

Sem isso não dá pra responder "meu deploy subiu?" — e a pergunta já custou
horas de arqueologia sondando rota por rota em produção.

Duas estratégias, nesta ordem:

1. **SHA do HEAD**, lido de `.git/` direto (sem depender do binário `git`).
   Cobre HEAD detached, ref solta e ref empacotada por `git gc`.

2. **Fingerprint do fonte**, quando não há `.git/`. É o caso real do
   EasyPanel, que busca o código por archive do GitHub — o diretório `.git/`
   simplesmente não existe no build context, então a estratégia 1 nunca teve
   chance ali. O fingerprint é um SHA-256 sobre caminho+conteúdo dos arquivos
   de aplicação: código idêntico dá fingerprint idêntico, e dá pra calcular o
   esperado localmente (`python scripts/resolve_git_commit.py --fingerprint`)
   e comparar com o que `/api/health` devolve.

Quebras de linha são normalizadas antes do hash: o repositório é clonado com
CRLF no Windows e o archive do GitHub entrega LF, e sem normalizar o mesmo
commit produziria fingerprints diferentes conforme a origem.
"""

from __future__ import annotations

import hashlib
import os
import sys

_NUL = b"\x00"
_CRLF = b"\x0d\x0a"
_LF = b"\x0a"


def resolver_commit(git_dir: str = ".git") -> str | None:
    head_path = os.path.join(git_dir, "HEAD")
    if not os.path.isfile(head_path):
        return None

    with open(head_path, encoding="utf-8") as f:
        head = f.read().strip()

    if not head.startswith("ref:"):
        return head or None  # HEAD detached — já é o SHA

    ref = head.split(" ", 1)[1].strip()
    ref_path = os.path.join(git_dir, ref)
    if os.path.isfile(ref_path):
        with open(ref_path, encoding="utf-8") as f:
            return f.read().strip() or None

    packed_refs_path = os.path.join(git_dir, "packed-refs")
    if os.path.isfile(packed_refs_path):
        with open(packed_refs_path, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha.endswith(" " + ref):
                    return linha.split()[0]

    return None


# Arquivos que definem o comportamento da aplicação. Config de lint, testes e
# documentação ficam de fora de propósito: mudar um teste não muda o que está
# rodando, e o fingerprint existe para responder sobre o que está rodando.
_RAIZES_FONTE = ("src",)
_ARQUIVOS_FONTE = ("pyproject.toml", "alembic.ini", "entrypoint.sh")


def _caminho_relativo(caminho: str, raiz: str) -> str:
    return os.path.relpath(caminho, raiz).replace(os.sep, "/")


def _arquivos_para_fingerprint(raiz: str) -> list[str]:
    encontrados: list[str] = []
    for sub in _RAIZES_FONTE:
        for pasta, _, arquivos in os.walk(os.path.join(raiz, sub)):
            if "__pycache__" in pasta:
                continue
            encontrados.extend(
                os.path.join(pasta, a) for a in arquivos if a.endswith(".py")
            )
    encontrados.extend(
        os.path.join(raiz, a)
        for a in _ARQUIVOS_FONTE
        if os.path.isfile(os.path.join(raiz, a))
    )
    # Ordena pelo caminho relativo normalizado: `os.walk` não garante ordem e o
    # separador de path difere entre Windows e Linux.
    return sorted(encontrados, key=lambda c: _caminho_relativo(c, raiz))


def fingerprint_fonte(raiz: str = ".") -> str:
    """SHA-256 de caminho+conteúdo dos arquivos de aplicação, em hex curto."""
    h = hashlib.sha256()
    for caminho in _arquivos_para_fingerprint(raiz):
        h.update(_caminho_relativo(caminho, raiz).encode("utf-8"))
        h.update(_NUL)
        with open(caminho, "rb") as f:
            h.update(f.read().replace(_CRLF, _LF))
        h.update(_NUL)
    return h.hexdigest()[:12]


def resolver_versao(raiz: str = ".") -> str:
    """SHA do commit quando há `.git/`; senão, fingerprint do fonte."""
    sha = resolver_commit(os.path.join(raiz, ".git"))
    if sha:
        return sha[:12]
    return "src-" + fingerprint_fonte(raiz)


if __name__ == "__main__":
    if "--fingerprint" in sys.argv:
        sys.stdout.write("src-" + fingerprint_fonte())
    else:
        sys.stdout.write(resolver_versao())
