"""Testes unitários — resolução de commit sem o binário git (build da imagem)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from resolve_git_commit import resolver_commit  # noqa: E402


def _git_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".git"
    d.mkdir()
    return d


def test_ref_solta(tmp_path: Path):
    git_dir = _git_dir(tmp_path)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    refs_heads = git_dir / "refs" / "heads"
    refs_heads.mkdir(parents=True)
    (refs_heads / "main").write_text("abc1234def5678\n")

    assert resolver_commit(str(git_dir)) == "abc1234def5678"


def test_ref_empacotada_packed_refs(tmp_path: Path):
    """Depois de um `git gc`, a ref pode não existir como arquivo solto —
    só dentro de packed-refs."""
    git_dir = _git_dir(tmp_path)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "9998887776665554443332221110009998887a refs/heads/outra\n"
        "abc1234def5678abc1234def5678abc1234def5 refs/heads/main\n"
    )

    assert resolver_commit(str(git_dir)) == "abc1234def5678abc1234def5678abc1234def5"


def test_head_detached(tmp_path: Path):
    git_dir = _git_dir(tmp_path)
    (git_dir / "HEAD").write_text("abc1234def5678abc1234def5678abc1234def5\n")

    assert resolver_commit(str(git_dir)) == "abc1234def5678abc1234def5678abc1234def5"


def test_sem_git_dir_retorna_none(tmp_path: Path):
    assert resolver_commit(str(tmp_path / "nao-existe")) is None


def test_ref_sem_arquivo_e_sem_packed_refs_retorna_none(tmp_path: Path):
    git_dir = _git_dir(tmp_path)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    # nem refs/heads/main solto, nem packed-refs

    assert resolver_commit(str(git_dir)) is None


def test_repo_real_bate_com_git_rev_parse():
    """No próprio repositório do contabil-core, o resultado precisa bater
    com `git rev-parse HEAD` — é o caso de uso real (build da imagem)."""
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    esperado = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()

    resultado = resolver_commit(str(repo_root / ".git"))
    assert resultado == esperado
