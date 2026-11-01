import importlib

import pytest

# The package tree from ARCHITECTURE.md; each subpackage must import on its own.
SUBPACKAGES = [
    "cli",
    "orchestration",
    "integrations.repocanon",
    "integrations.repokg",
    "integrations.reflex",
    "models.apple",
    "models.mlx",
    "routing",
    "context",
    "provenance",
    "repository",
    "lsp",
    "tools.filesystem",
    "tools.git",
    "tools.shell",
    "tools.languages",
    "tools.docker",
    "tools.kubernetes",
    "tools.blockchain",
    "execution",
    "processes",
    "services",
    "policy",
    "security",
    "secrets",
    "sessions",
    "recovery",
    "observability",
    "config",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    module = importlib.import_module(f"dfm.{name}")
    assert module.__doc__, f"dfm.{name} needs a one-line docstring"
