from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.integrations.repokg import RepoKGProvider
from dfm.integrations.repokg.structure import graph_at_ref, roles_from_canon, structural_diff
from tests import repo_fixtures as fx

BASE = {
    "core/__init__.py": "",
    "core/model.py": "VALUE = 1\n",
    "api/__init__.py": "",
    "api/routes.py": "from core import model\n",
    "tests/__init__.py": "",
    "tests/test_model.py": "from core import model\n",
}


@pytest.fixture
def repo(git_repo: Path, git: Callable[..., str]) -> Path:
    fx.write(git_repo, BASE)
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "base")
    return git_repo


def kg(nodes: list[str], edges: list[tuple[str, str]]) -> dict[str, object]:
    return {
        "repokg_version": 1,
        "modules": [{"path": n, "lang": "Python", "files": 1, "loc": 1} for n in nodes],
        "edges": [{"from": a, "to": b, "lang": "Python", "count": 1} for a, b in edges],
        "languages": [],
        "ops": {"workflows": []},
    }


def test_no_change() -> None:
    graph = kg(["a", "b"], [("a", "b")])
    delta = structural_diff(graph, graph)
    assert delta.empty and not delta.shape_changed
    assert delta.markdown().startswith("No structural change")


def test_added_removed_and_renamed() -> None:
    before = kg(["a", "b"], [("a", "b")])
    after = kg(["a", "c", "d"], [("a", "c"), ("d", "a")])
    delta = structural_diff(before, after)
    assert delta.shape_changed
    assert set(delta.added_modules) == {"c", "d"}
    assert delta.removed_modules == ["b"]
    assert delta.renamed_modules and delta.renamed_modules[0].startswith("b -> c")
    assert "d -> a" in delta.added_edges and "a -> b" in delta.removed_edges
    assert "New dependencies" in delta.markdown()


def test_boundary_warning() -> None:
    before = kg(["src", "tests"], [("tests", "src")])
    after = kg(["src", "tests"], [("tests", "src"), ("src", "tests")])
    delta = structural_diff(before, after, roles={"src": "source", "tests": "test"})
    assert [w.edge for w in delta.boundary_warnings] == ["src -> tests"]
    assert "Architecture warnings" in delta.markdown()


async def test_working_tree_vs_head(repo: Path, git: Callable[..., str]) -> None:
    fx.write(repo, {"web/__init__.py": "", "web/app.py": "from api import routes\n"})
    (repo / "core" / "model.py").write_text("from tests import test_model\n")
    before = await graph_at_ref(repo, "HEAD")
    after = await RepoKGProvider().graph(repo)
    assert before is not None and after is not None
    roles = {"core": "source", "tests": "test", "api": "source", "web": "source"}
    delta = structural_diff(before, after, roles)
    assert "web" in delta.added_modules
    assert "web -> api" in delta.added_edges
    assert [w.edge for w in delta.boundary_warnings] == ["core -> tests"]
    # the temporary checkout is gone and the user's tree was not touched
    assert git(repo, "worktree", "list").count("\n") == 1
    assert "core/model.py" in git(repo, "status", "--porcelain")


async def test_missing_ref(repo: Path) -> None:
    assert await graph_at_ref(repo, "no-such-ref") is None


def test_roles_from_canon_degraded() -> None:
    assert roles_from_canon(object()) == {}


def test_cli_diff_structure(repo: Path, tmp_path: Path) -> None:
    fx.write(repo, {"web/__init__.py": "", "web/app.py": "from api import routes\n"})
    result = CliRunner().invoke(
        app, ["diff", "--structure", "--repo", str(repo)], env={"HOME": str(tmp_path / "h")}
    )
    assert result.exit_code == 0, result.output
    assert "web -> api" in result.stdout
