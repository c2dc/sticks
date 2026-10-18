"""Static assertion test for the STIX ``.gitignore`` rules (task 6.1).

Mesmo com o default do cache STIX movido para fora do repositório
(``%LOCALAPPDATA%\\sticks\\stix`` / ``$XDG_CACHE_HOME/sticks/stix`` /
``~/.cache/sticks/stix``), as regras de ``.gitignore`` que cobrem bundles STIX
dentro da árvore do repositório **permanecem** como defesa em profundidade — de
modo que um download acidental para o layout antigo nunca volte a ser versionado.

Este teste é uma asserção estática (não-propriedade): lê o ``.gitignore`` da raiz
do repositório e verifica que as linhas ``packages/cli/data/stix/*.json`` e
``sticks/data/stix/*.json`` continuam presentes.

_Requirements: 4.2_
"""

from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    """tests/ -> backend/ -> apps/ -> repo root (contém o ``.gitignore`` raiz)."""
    return Path(__file__).resolve().parents[3]


def _gitignore_lines() -> list[str]:
    gitignore = _repo_root() / ".gitignore"
    assert gitignore.is_file(), f".gitignore not found at {gitignore}"
    return [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()]


def test_gitignore_keeps_packages_cli_stix_rule() -> None:
    # Defesa em profundidade para o layout antigo (packages/cli/data/stix).
    assert "packages/cli/data/stix/*.json" in _gitignore_lines()


def test_gitignore_keeps_sticks_stix_rule() -> None:
    # Defesa em profundidade para o layout alternativo (sticks/data/stix).
    assert "sticks/data/stix/*.json" in _gitignore_lines()
