from pathlib import Path

from dfm.routing import Route, RouteDecision, RoutingContext
from dfm.routing.evidence import RoutingEvidence, task_class
from dfm.sessions import SessionStore


def record(
    store: SessionStore, repo: Path, request: str, coder: str, status: str, mode: str = "implement"
) -> None:
    session = store.create(repo, request, mode=mode)
    store.append(
        session, "route_decision", route="PRIMARY_CODER", source="deterministic", coder=coder
    )
    store.complete(session, status)  # type: ignore[arg-type]


def decision(coder: str) -> RouteDecision:
    route = Route.SMALL_CODER if coder == "small" else Route.PRIMARY_CODER
    return RouteDecision(route, ("rule",), "deterministic", 0.8, coder)  # type: ignore[arg-type]


def test_task_classes() -> None:
    assert task_class("fix the failing tests") == "tests"
    assert task_class("the CI pipeline is red") == "ci"
    assert task_class("refactor the auth module") == "refactor"
    assert task_class("fix the crash on startup") == "bugfix"
    assert task_class("implement issue 42") == "feature"
    assert task_class("tell me a story") == "other"


def test_upgrade_when_small_keeps_failing(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    repo = tmp_path / "repo"
    repo.mkdir()
    for status in ("failed", "failed", "completed", "failed"):
        record(store, repo, "fix the crash in the parser", "small", status)
    record(store, repo, "fix the crash in io", "primary", "completed")
    adjusted = RoutingEvidence(store, repo).adjust(
        decision("small"), "fix the crash in the lexer", RoutingContext()
    )
    assert adjusted.coder == "primary" and adjusted.route is Route.PRIMARY_CODER
    assert adjusted.source == "reflex" and "1/4 bugfix" in adjusted.reasons[-1]


def test_downgrade_when_small_is_reliable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    repo = tmp_path / "repo"
    repo.mkdir()
    for _ in range(5):
        record(store, repo, "fix the failing tests", "small", "completed")
    adjusted = RoutingEvidence(store, repo).adjust(
        decision("primary"), "fix the failing tests in api", RoutingContext()
    )
    assert adjusted.coder == "small" and "faster" in adjusted.reasons[-1]


def test_no_change_without_enough_evidence_or_for_overrides(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    repo = tmp_path / "repo"
    repo.mkdir()
    record(store, repo, "fix the crash", "small", "failed")
    evidence = RoutingEvidence(store, repo)
    assert evidence.adjust(decision("small"), "fix the crash", RoutingContext()).coder == "small"
    override = RouteDecision(Route.SMALL_CODER, ("user override",), "override", 1.0, "small")
    assert evidence.adjust(override, "fix the crash", RoutingContext()) is override
    tools = RouteDecision(Route.TOOLS_ONLY, ("git",), "deterministic", 0.9)
    assert evidence.adjust(tools, "git status", RoutingContext()) is tools


def test_other_repos_and_modes_ignored(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    for _ in range(4):
        record(store, other, "fix the crash", "small", "failed")
        record(store, repo, "fix the crash", "small", "failed", mode="plan")
    assert RoutingEvidence(store, repo).stats() == {}
    assert RoutingEvidence(store).stats()[("bugfix", "small")].sessions == 4
