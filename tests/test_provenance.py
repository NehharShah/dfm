import hashlib
from pathlib import Path

from dfm.provenance import ContextItem
from dfm.sessions import SessionStore


def test_make_redacts_and_hashes_redacted_text() -> None:
    item = ContextItem.make("repo", "token = 'ghp_" + "a" * 36 + "'", "config file", path="cfg.py")
    assert "ghp_" not in item.content
    assert item.content_hash == hashlib.sha256(item.content.encode()).hexdigest()


def test_location_and_tokens() -> None:
    item = ContextItem.make("lsp", "def f(): ...", "definition", path="a.py", line_range=(3, 9))
    assert item.location == "a.py:3-9"
    assert item.est_tokens >= 1
    assert ContextItem.make("user", "hi", "request").location == "user"


def test_provenance_roundtrip_without_content() -> None:
    item = ContextItem.make(
        "test", "FAILED test_add", "failing test", path="t.py", line_range=(1, 2), priority=2
    )
    data = item.provenance()
    assert "content" not in data
    back = ContextItem.from_provenance(data, item.content)
    assert back == item


def test_historical_label() -> None:
    reflex = ContextItem.make("reflex", "Run cargo nextest for integration tests", "practice")
    assert reflex.historical
    rendered = reflex.render()
    assert "historical evidence" in rendered and 'trust="historical"' in rendered
    assert not ContextItem.make("repocanon", "x", "y").historical
    assert 'trust="deterministic"' in ContextItem.make("repocanon", "x", "y").render()


def test_trace_records_provenance_only(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    session = store.create(tmp_path, "x")
    item = ContextItem.make("repo", "SECRET_BODY_TEXT", "file", path="a.py")
    store.append(session, "context_selected", items=[item.provenance()])
    raw = session.path.read_text()
    assert "SECRET_BODY_TEXT" not in raw
    assert item.content_hash in raw
