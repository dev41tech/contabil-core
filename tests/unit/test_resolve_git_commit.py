"""Testes unitários — identificação da versão do código que virou a imagem.

Cobre os dois caminhos: SHA lido de `.git/` e, quando ele não existe (o caso
real do EasyPanel, que busca por archive do GitHub), o fingerprint do fonte.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from resolve_git_commit import (  # noqa: E402
    fingerprint_fonte,
    resolver_commit,
    resolver_versao,
)


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


# ── Fingerprint do fonte — o caminho que o EasyPanel realmente usa ───────────
#
# O deploy busca o código por archive do GitHub, sem `.git/` nenhum. Todo o
# resolvedor por SHA acima nunca teve chance nesse ambiente, e o health ficou
# meses respondendo "unknown" — o que custou uma investigação inteira sondando
# rota por rota em produção para descobrir se um deploy tinha subido.


def _montar_fonte(raiz: Path, quebra: bytes = b"\x0a", extra: str = "") -> None:
    (raiz / "src" / "core").mkdir(parents=True)
    arquivos = {
        "src/core/config.py": b"x = 1",
        "src/core/texto.py": b"y = 2",
        "pyproject.toml": b"[projeto]",
    }
    for caminho, conteudo in arquivos.items():
        destino = raiz / caminho
        destino.write_bytes(quebra.join([conteudo, extra.encode() or b"z = 3", b""]))


def test_fingerprint_e_estavel_para_o_mesmo_conteudo(tmp_path: Path):
    """Duas cópias do mesmo código têm que dar o mesmo fingerprint — é o que
    permite calcular o esperado localmente e comparar com produção."""
    a, b = tmp_path / "a", tmp_path / "b"
    _montar_fonte(a)
    _montar_fonte(b)

    assert fingerprint_fonte(str(a)) == fingerprint_fonte(str(b))


def test_fingerprint_muda_quando_o_codigo_muda(tmp_path: Path):
    """Sem isto o identificador não serviria para nada: um deploy novo com
    código diferente precisa aparecer diferente."""
    a, b = tmp_path / "a", tmp_path / "b"
    _montar_fonte(a)
    _montar_fonte(b, extra="z = 99")

    assert fingerprint_fonte(str(a)) != fingerprint_fonte(str(b))


def test_fingerprint_ignora_crlf_versus_lf(tmp_path: Path):
    """O repositório é clonado com CRLF no Windows e o archive do GitHub
    entrega LF. Sem normalizar, o mesmo commit daria identificadores
    diferentes conforme a origem — e a comparação com produção perderia o
    sentido justamente no ambiente para o qual ela existe."""
    crlf, lf = tmp_path / "crlf", tmp_path / "lf"
    _montar_fonte(crlf, quebra=b"\x0d\x0a")
    _montar_fonte(lf, quebra=b"\x0a")

    assert fingerprint_fonte(str(crlf)) == fingerprint_fonte(str(lf))


def test_resolver_versao_sem_git_cai_no_fingerprint(tmp_path: Path):
    """O cenário do EasyPanel: build context sem `.git/`. Antes isto devolvia
    "unknown" e o deploy virava adivinhação."""
    _montar_fonte(tmp_path)

    versao = resolver_versao(str(tmp_path))

    assert versao.startswith("src-")
    assert versao != "unknown"
    assert versao.endswith(fingerprint_fonte(str(tmp_path)))


def test_resolver_versao_prefere_o_sha_quando_ha_git(tmp_path: Path):
    """Com `.git/` disponível o SHA é mais útil que o fingerprint: aponta para
    um commit navegável no GitHub."""
    _montar_fonte(tmp_path)
    git_dir = _git_dir(tmp_path)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "main").write_text("a" * 40 + "\n")

    assert resolver_versao(str(tmp_path)) == "a" * 12


# ── A ponte entre o build e o health ────────────────────────────────────────


def test_settings_le_a_versao_do_arquivo_escrito_no_build(tmp_path, monkeypatch):
    """A lacuna que mantinha o health em "unknown" mesmo com o resolvedor certo.

    O Dockerfile escrevia o valor em `/app/.git_commit`, mas nada no código
    lia esse arquivo — e um `RUN` não consegue alterar o `ENV GIT_COMMIT` já
    definido na imagem, então o valor resolvido no build não tinha como chegar
    até a aplicação.
    """
    from src.core.config import Settings

    arquivo = tmp_path / ".git_commit"
    arquivo.write_text("src-19a5a3f2d8b5\n")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT_FALLBACK_FILE", str(arquivo))

    assert Settings().git_commit == "src-19a5a3f2d8b5"


def test_build_arg_explicito_tem_prioridade_sobre_o_arquivo(tmp_path, monkeypatch):
    """Quem passa `--build-arg GIT_COMMIT` está dizendo exatamente o que quer
    ver no health; o arquivo é só o plano B."""
    from src.core.config import Settings

    arquivo = tmp_path / ".git_commit"
    arquivo.write_text("src-19a5a3f2d8b5\n")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("GIT_COMMIT", "571b878bb69f")
    monkeypatch.setenv("GIT_COMMIT_FALLBACK_FILE", str(arquivo))

    assert Settings().git_commit == "571b878bb69f"


def test_arquivo_ausente_nao_derruba_a_aplicacao(tmp_path, monkeypatch):
    """Health degradado é ruim; container que não sobe é pior."""
    from src.core.config import Settings

    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT_FALLBACK_FILE", str(tmp_path / "nao-existe"))

    assert Settings().git_commit == "unknown"
