import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import dfm.tools.filesystem.tools as tools_mod
from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.policy import Approval, Policy, PolicyVerdict, ProposedAction
from dfm.recovery import Checkpointer
from dfm.tools.filesystem import (
    FsTools,
    PatchConflict,
    SearchReplace,
    ToolError,
    UnifiedDiff,
    apply_hunks,
    apply_search_replace,
    parse_unified,
)
from tests import repo_fixtures as fx

SOURCE = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"


class Approve:
    def __init__(self, answer: Approval) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        self.asked.append(action.summary)
        return self.answer


@pytest.fixture
def repo(git_repo: Path) -> Path:
    fx.write(git_repo, {"src/calc.py": SOURCE, "data.bin": "", ".env": "KEY=value\n"})
    (git_repo / "data.bin").write_bytes(b"\x00\x01\x02")
    return git_repo


def make_tools(
    repo: Path, tmp_path: Path, approver: Approve | None = None, **kw: object
) -> FsTools:
    environ = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    env = ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="fs", environ=environ, dirs=state_dirs(environ)
    )
    return FsTools(env, Policy(DFMConfig(), approver), **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- patch algorithms


def test_search_replace_exact() -> None:
    out = apply_search_replace(
        SOURCE,
        [SearchReplace("def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b")],
    )
    assert "return a + b" in out.text and out.relocated == 0
    assert out.text.count("return a - b") == 1


def test_search_replace_whole_lines_with_trailing_newline() -> None:
    text = "fn add(a: i32, b: i32) -> i32 {\n    a - b\n}\n"
    out = apply_search_replace(text, [SearchReplace("    a - b\n", "    a + b\n")])
    assert out.text == "fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
    dropped = apply_search_replace(text, [SearchReplace("    a - b\n", "")])
    assert dropped.text == "fn add(a: i32, b: i32) -> i32 {\n}\n"


def test_search_replace_ambiguous_and_missing() -> None:
    with pytest.raises(PatchConflict, match="occurs 2 times"):
        apply_search_replace(SOURCE, [SearchReplace("    return a - b", "    return 0")])
    with pytest.raises(PatchConflict, match="not found") as info:
        apply_search_replace(SOURCE, [SearchReplace("def add(x, y):\n    return x - y", "z")])
    assert info.value.candidates and "lines 1-2" in info.value.candidates[0]


def test_search_replace_relocates_whitespace_drift() -> None:
    text = "class C:\n    def add(self, a, b):\n        return a - b\n"
    out = apply_search_replace(
        text,
        [
            SearchReplace(
                "def add(self, a, b):\n    return a - b", "def add(self, a, b):\n    return a + b"
            )
        ],
    )
    assert out.relocated == 1
    assert "        return a + b" in out.text  # indentation adjusted to the real location


def test_unified_diff_with_offset() -> None:
    shifted = "# header\n# more\n" + SOURCE
    diff = (
        "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a - b\n+    return a + b\n"
    )
    hunks = parse_unified(diff)[0].hunks
    out = apply_hunks(shifted, hunks)
    assert "return a + b" in out.text
    assert any("offset" in n for n in out.notes)


def test_zero_context_hunks_insert_after_the_named_line() -> None:
    """`diff -U0` writes `@@ -N,0 +M,k @@`: N is the line the insertion follows."""
    source = "l1\nl2\nl3\nl4\nl5\n"
    after_three = "--- a/f\n+++ b/f\n@@ -3,0 +4 @@\n+INSERTED\n"
    out = apply_hunks(source, parse_unified(after_three)[0].hunks)
    assert out.text == "l1\nl2\nl3\nINSERTED\nl4\nl5\n"
    at_top = "--- a/f\n+++ b/f\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    assert apply_hunks(source, parse_unified(at_top)[0].hunks).text == "a\nb\n" + source
    two = "--- a/f\n+++ b/f\n@@ -1,0 +2 @@\n+X\n@@ -4,0 +6 @@\n+Y\n"
    assert apply_hunks(source, parse_unified(two)[0].hunks).text == "l1\nX\nl2\nl3\nl4\nY\nl5\n"


def test_unified_diff_conflict() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-nothing like this\n+x\n"
    with pytest.raises(PatchConflict, match="context not found"):
        apply_hunks(SOURCE, parse_unified(diff)[0].hunks)


def test_parse_requires_headers() -> None:
    with pytest.raises(PatchConflict):
        parse_unified("@@ -1 +1 @@\n-a\n+b\n")


# ---------------------------------------------------------------- tools


async def test_read_and_list_and_search(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    sl = await fs.read_lines("src/calc.py", 1, 2)
    assert sl.text == "def add(a, b):\n    return a - b"
    assert sl.total_lines == 6 and len(sl.sha256) == 64
    assert "src/" in await fs.list_directory(".")
    assert await fs.search_files("*.py") == ["app.py", "src/calc.py"]
    assert await fs.search_files("*.py", "src") == ["src/calc.py"]
    env_view = await fs.read_file(".env")
    assert "value" not in env_view.text
    blob = await fs.read_file("data.bin")  # 054: a stub describing it, never the bytes
    assert blob.stub and blob.text.startswith("[data.bin: binary,")
    with pytest.raises(ToolError):
        await fs.read_file("../outside.txt")


@pytest.mark.parametrize("use_rg", [True, False])
async def test_search_text(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_rg: bool
) -> None:
    if use_rg and tools_mod._real_rg() is None:  # pyright: ignore[reportPrivateUsage]
        pytest.skip("ripgrep binary not installed")
    if not use_rg:
        monkeypatch.setattr(tools_mod, "_real_rg", lambda: None)
    matches = await make_tools(repo, tmp_path).search_text(r"def \w+", "src")
    assert [(m.path, m.line) for m in matches] == [("src/calc.py", 1), ("src/calc.py", 5)]
    with pytest.raises(ToolError, match=r"regex|search failed"):  # not a crash, not "no matches"
        await make_tools(repo, tmp_path).search_text("add(a", "src")
    literal = await make_tools(repo, tmp_path).search_text("add(a", "src", regex=False)
    assert [m.line for m in literal] == [1]


async def test_ripgrep_results_skip_secret_files_and_timeouts_are_observations(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a stand-in rg that prints what real rg would, including a hit inside a key file
    (repo / "server.key").write_text("TOKEN=abc\n")
    fake = tmp_path / "rg"
    fake.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{repo}/server.key:1:TOKEN=abc' '{repo}/src/calc.py:1:def add'\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(tools_mod, "_real_rg", lambda: str(fake))
    matches = await make_tools(repo, tmp_path).search_text("TOKEN|def", ".")
    assert [(m.path, m.line) for m in matches] == [("src/calc.py", 1)]

    def too_slow(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("rg", 60)

    monkeypatch.setattr(tools_mod.subprocess, "run", too_slow)
    with pytest.raises(ToolError, match="timed out"):
        await make_tools(repo, tmp_path).search_text("x", ".")


async def test_patch_file_and_checkpoint(repo: Path, tmp_path: Path) -> None:
    (repo / "src/calc.py").chmod(0o640)
    checkpointer = Checkpointer(repo, "s1")
    events: list[str] = []
    fs = make_tools(
        repo, tmp_path, checkpointer=checkpointer, on_event=lambda t, **_: events.append(t)
    )
    result = await fs.patch_file(
        "src/calc.py",
        [SearchReplace("def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b")],
    )
    assert result.applied and result.before_sha != result.after_sha
    assert "return a + b" in (repo / "src/calc.py").read_text()
    assert stat.S_IMODE((repo / "src/calc.py").stat().st_mode) == 0o640
    assert checkpointer.original_content("src/calc.py") == SOURCE.encode()
    assert events == ["patch_started", "patch_finished"]
    assert fs.modified == {"src/calc.py"}


async def test_patch_conflict_never_rewrites(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    result = await fs.patch_file("src/calc.py", [SearchReplace("def mul(a, b):", "x")])
    assert not result.applied and result.conflict
    assert (repo / "src/calc.py").read_text() == SOURCE


async def test_stale_pre_image(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    result = await fs.patch_file(
        "src/calc.py", [SearchReplace("a - b", "a + b")], expected_sha="0" * 64
    )
    assert not result.applied and "re-read" in (result.conflict or "")


async def test_refuses_redaction_markers(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    with pytest.raises(ToolError, match="redaction"):
        await fs.write_file("cfg.py", "API_KEY = '[REDACTED]'\n")
    with pytest.raises(ToolError, match="redaction"):
        await fs.patch_file(
            "src/calc.py",
            [SearchReplace("def sub(a, b):", "TOKEN = '[REDACTED_TOKEN]'\ndef sub(a, b):")],
        )


async def test_writes_respect_boundary_and_git_internals(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    with pytest.raises(ToolError):
        await fs.write_file("../escape.txt", "x")
    with pytest.raises(ToolError, match="Git internals"):
        await fs.write_file(".git/hooks/pre-commit", "x")


async def test_unified_new_file_and_create(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    diff = "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1,2 @@\n+X = 1\n+Y = 2\n"
    result = await fs.patch_file("src/new.py", UnifiedDiff(diff))
    assert result.applied and (repo / "src/new.py").read_text() == "X = 1\nY = 2\n"
    with pytest.raises(ToolError, match="already exists"):
        await fs.create_file("src/new.py", "again")


async def test_delete_needs_approval_unless_created(repo: Path, tmp_path: Path) -> None:
    approver = Approve(Approval.DENY)
    fs = make_tools(repo, tmp_path, approver)
    await fs.create_file("tmp_notes.txt", "x")
    await fs.delete_file("tmp_notes.txt")  # DFM created it: no question asked
    assert approver.asked == []
    with pytest.raises(ToolError, match="policy denied"):
        await fs.delete_file("src/calc.py")
    assert approver.asked == ["delete src/calc.py"]
    assert (repo / "src/calc.py").exists()


async def test_secret_file_writes_ask(repo: Path, tmp_path: Path) -> None:
    approver = Approve(Approval.DENY)
    fs = make_tools(repo, tmp_path, approver)
    with pytest.raises(ToolError, match="policy denied"):
        await fs.write_file(".env", "KEY=other\n")
    assert (repo / ".env").read_text() == "KEY=value\n"


async def test_guard_veto(repo: Path, tmp_path: Path) -> None:
    from dfm.policy import ActionClass, Decision

    def guard(rel: str) -> PolicyVerdict | None:
        return PolicyVerdict(
            Decision.DENY, ActionClass.WORKTREE_WRITE, "had uncommitted user changes"
        )

    fs = make_tools(repo, tmp_path, guard=guard)
    with pytest.raises(ToolError, match="uncommitted user changes"):
        await fs.write_file("src/calc.py", "x = 1\n")


async def test_move(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    await fs.move_file("src/calc.py", "src/arith.py")
    assert (repo / "src/arith.py").exists() and not (repo / "src/calc.py").exists()
    with pytest.raises(ToolError, match="does not exist"):
        await fs.move_file("src/calc.py", "x.py")


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive file system")
async def test_git_internals_and_protected_paths_ignore_case(repo: Path, tmp_path: Path) -> None:
    fs = make_tools(repo, tmp_path)
    with pytest.raises(ToolError):  # .GIT is .git on APFS
        await fs.write_file(".GIT/hooks/pre-commit", "#!/bin/sh\nevil\n")
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


async def test_python_search_skips_symlinks_out_of_the_repo(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools_mod, "_real_rg", lambda: None)
    outside = tmp_path / "aws_credentials"
    outside.write_text("aws_secret_access_key = abc\n")
    (repo / "notes.txt").symlink_to(outside)
    matches = await make_tools(repo, tmp_path).search_text("aws_secret", ".")
    assert matches == []


def test_removed_sql_comment_lines_stay_in_the_hunk() -> None:
    diff = (
        "--- a/q.sql\n"
        "+++ b/q.sql\n"
        "@@ -1,3 +1,2 @@\n"
        "--- old comment\n"
        " select 1;\n"
        "-select 2;\n"
        "+select 3;\n"
    )
    (only,) = parse_unified(diff)
    (hunk,) = only.hunks
    assert hunk.old == ["-- old comment", "select 1;", "select 2;"]
    assert hunk.new == ["select 1;", "select 3;"]


async def test_patching_a_latin1_file_keeps_its_other_bytes(repo: Path, tmp_path: Path) -> None:
    path = repo / "legacy.txt"
    path.write_bytes("café = 1\nvalue = 2\n".encode("latin-1"))
    result = await make_tools(repo, tmp_path).patch_file(
        "legacy.txt", [SearchReplace("value = 2", "value = 3")]
    )
    assert result.applied
    assert path.read_bytes() == "café = 1\nvalue = 3\n".encode("latin-1")  # é untouched


async def test_new_files_follow_the_umask(repo: Path, tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        await make_tools(repo, tmp_path).create_file("made.txt", "x\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE((repo / "made.txt").stat().st_mode) == 0o644


async def test_an_edit_during_the_approval_wait_is_not_overwritten(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = make_tools(repo, tmp_path)
    real_prepare = fs._prepare_write  # pyright: ignore[reportPrivateUsage]

    async def developer_edits_meanwhile(*args: object, **kwargs: object) -> None:
        await real_prepare(*args, **kwargs)  # type: ignore[arg-type]
        (repo / "src/calc.py").write_text(SOURCE + "# developer note\n")

    monkeypatch.setattr(fs, "_prepare_write", developer_edits_meanwhile)
    result = await fs.patch_file(
        "src/calc.py",
        [SearchReplace("def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b")],
    )
    assert not result.applied and "while waiting for approval" in (result.conflict or "")
    assert (repo / "src/calc.py").read_text().endswith("# developer note\n")
