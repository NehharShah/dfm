"""Docs stay true: generated references match the code, relative links resolve."""

import re
from pathlib import Path

import pytest

from dfm import docsgen

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("name", sorted(docsgen.TARGETS))
def test_generated_reference_is_current(name: str) -> None:
    current = (ROOT / "docs" / name).read_text()
    assert current == docsgen.TARGETS[name](), f"docs/{name} is stale: run `python -m dfm.docsgen`"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(page: Path) -> None:
    for target in LINK.findall(page.read_text()):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        assert (page.parent / path).exists(), f"{page.relative_to(ROOT)}: broken link {target}"


def test_every_command_is_documented() -> None:
    from typer.main import get_command

    from dfm.cli.app import app

    reference = (ROOT / "docs" / "cli.md").read_text()
    for name in getattr(get_command(app), "commands", {}):
        assert f"## dfm {name}\n" in reference, name


def test_config_reference_covers_every_key() -> None:
    from dfm.config import DFMConfig
    from dfm.config.loader import flatten

    reference = (ROOT / "docs" / "config.md").read_text()
    for key in flatten(DFMConfig().model_dump()):
        assert f"`{key}`" in reference, key


def test_threat_model_and_threat_suite_stay_in_step() -> None:
    """Every `test_tN_*` in the threat suite is in the regression table, and every threat in the
    table has a test named for it (T4/T7 share one; that is fine)."""
    doc = (ROOT / "docs" / "threat-model.md").read_text()
    suite = (ROOT / "tests" / "test_threats.py").read_text()
    tests = set(re.findall(r"^(?:async )?def (test_t\d+[a-z0-9_]*)\(", suite, re.M))
    threats = set(re.findall(r"^\| (T\d+) \|", doc, re.M))
    listed = set(re.findall(r"`(test_t\d+[a-z0-9_]*)`", doc))
    assert tests == listed, {"only in suite": tests - listed, "only in doc": listed - tests}
    for threat in threats:
        number = threat[1:]
        assert any(re.search(rf"_t(\d+_)*{number}(_|$)", t) for t in tests), threat
