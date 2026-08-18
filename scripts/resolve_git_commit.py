"""Resolve o SHA curto do HEAD lendo `.git/` direto, sem precisar do binário `git`.

Existe porque o pipeline de deploy não estava passando `--build-arg
GIT_COMMIT`, então `GET /api/health` sempre respondia "unknown" — não dava
pra confirmar remotamente qual commit estava rodando depois de um deploy.
Chamado pelo Dockerfile antes de `.git/` ser removido da imagem final.

Cobre HEAD detached, ref solta (comum) e ref empacotada por `git gc`
(packed-refs) — os dois formatos que `.git/HEAD` pode apontar para.
"""

from __future__ import annotations

import os
import sys


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


if __name__ == "__main__":
    sha = resolver_commit()
    sys.stdout.write((sha or "unknown")[:12])
