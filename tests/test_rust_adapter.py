import os
import pwd
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.integrations.repocanon import RepoCanonProvider
from dfm.lsp import LspClient
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from dfm.processes import ProcessResult
from dfm.tools.filesystem import FsTools, SearchReplace
from dfm.tools.languages import ADAPTERS, RustAdapter
from dfm.tools.languages.rust import crate_for, workspace_members
from dfm.tools.results import FailedTest
from dfm.tools.shell.runner import CommandRunner
from tests import repo_fixtures as fx
from tests.helpers import DONE, ScriptedCoder, sandboxed_runner

FIXTURES = Path(__file__).parent / "fixtures"
CARGO = shutil.which("cargo")
cargo_only = pytest.mark.skipif(CARGO is None, reason="cargo not installed")


def workspace(root: Path) -> Path:
    return fx.write(
        root,
        {
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\nexclude = ["crates/old"]\n',
            "crates/core/Cargo.toml": '[package]\nname = "core-lib"\nversion = "0.1.0"\n',
            "crates/core/src/lib.rs": "",
            "crates/cli/Cargo.toml": '[package]\nname = "cli"\nversion = "0.1.0"\n',
            "crates/cli/src/main.rs": "fn main() {}\n",
            "crates/old/Cargo.toml": '[package]\nname = "old"\nversion = "0.1.0"\n',
        },
    )


def test_workspace_members_and_owning_crate(tmp_path: Path) -> None:
    repo = workspace(tmp_path / "ws")
    assert workspace_members(repo) == {"cli": "crates/cli", "core-lib": "crates/core"}
    assert crate_for(repo, "crates/core/src/lib.rs") == "core-lib"
    assert crate_for(repo, "README.md") is None
    single = fx.rust(tmp_path / "one")
    assert crate_for(single, "src/lib.rs") == "demo"


def test_default_commands(tmp_path: Path) -> None:
    repo = workspace(tmp_path / "ws")
    adapter = RustAdapter()
    assert adapter.detect(repo, None)
    assert any(isinstance(a, RustAdapter) for a in ADAPTERS)
    cmds = adapter.commands(repo, None, "crates/core/src/lib.rs")
    assert cmds.test is not None and cmds.test.argv == ("cargo", "test", "-p", "core-lib")
    assert cmds.check is not None and "--message-format=json" in cmds.check.argv
    assert cmds.reporter == "cargo"


async def test_repocanon_command_for_owning_crate(tmp_path: Path) -> None:
    repo = workspace(tmp_path / "ws")
    canon = await RepoCanonProvider().analyze(repo)
    cmds = RustAdapter().commands(repo, canon, "crates/cli/src/main.rs")
    assert cmds.test is not None and cmds.test.source == "repocanon"
    assert cmds.test.argv[:2] == ("cargo", "test") and cmds.test.cwd == "crates/cli"
    assert cmds.check is not None and cmds.check.cwd == "crates/cli"


def test_targeted_test_keeps_separator_args(tmp_path: Path) -> None:
    from dfm.tools.languages import ToolchainCommands, ToolCommand

    adapter = RustAdapter()
    cmds = ToolchainCommands(
        "Rust", test=ToolCommand(("cargo", "test", "--", "--nocapture")), reporter="cargo"
    )
    t = adapter.targeted_test(tmp_path, cmds, FailedTest("tests::adds", "x"))
    assert t is not None
    assert t.argv == ("cargo", "test", "tests::adds", "--", "--nocapture", "--exact")


def test_golden_outputs() -> None:
    adapter = RustAdapter()
    cmds = adapter.commands(FIXTURES / "rust_calc", None)
    text = (FIXTURES / "test_outputs" / "cargo_test.txt").read_text()
    report = adapter.parse_tests(ProcessResult(("cargo", "test"), 101, text, "", 10), cmds)
    assert [f.id for f in report.failed] == ["tests::adds"]
    assert (report.failed[0].file, report.failed[0].line) == ("src/lib.rs", 9)
    check = (FIXTURES / "test_outputs" / "cargo_check.json").read_text()
    diags = adapter.parse_diagnostics(
        ProcessResult(("cargo", "check"), 101, check, "", 10), "rustc"
    )
    assert diags and (diags[0].file, diags[0].line, diags[0].code) == ("src/lib.rs", 1, "E0277")


def test_rustc_text_diagnostics_when_tests_do_not_build() -> None:
    stderr = (
        "   Compiling calc v0.1.0\n"
        "error[E0308]: mismatched types\n"
        " --> src/lib.rs:2:5\n"
        '  |\n2 |     "x"\n  |     ^^^ expected `i32`, found `&str`\n'
        "error: could not compile `calc` (lib test) due to 1 previous error\n"
    )
    adapter = RustAdapter()
    cmds = adapter.commands(FIXTURES / "rust_calc", None)
    report = adapter.parse_tests(ProcessResult(("cargo", "test"), 101, "", stderr, 10), cmds)
    assert not report.ran
    assert any("src/lib.rs:2:5: mismatched types" in e for e in report.errors)


# ---------------------------------------------------------------- live: real cargo through the sandbox


def toolchain_env() -> dict[str, str]:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return {
        "CARGO_HOME": os.environ.get("CARGO_HOME", str(real_home / ".cargo")),
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(real_home / ".rustup")),
    }


def make_runner(repo: Path, tmp_path: Path) -> CommandRunner:
    assert CARGO is not None
    return sandboxed_runner(repo, tmp_path, str(Path(CARGO).parent), env=toolchain_env())


@cargo_only
async def test_live_fail_parse_fix_pass(tmp_path: Path) -> None:
    repo = tmp_path / "calc"
    shutil.copytree(FIXTURES / "rust_calc", repo)
    adapter = RustAdapter()
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None
    runner = make_runner(repo, tmp_path)
    outcome = await runner.run(cmds.test.argv, timeout=300)
    assert outcome.result is not None, outcome
    report = adapter.parse_tests(outcome.result, cmds)
    assert report.ran and [f.id for f in report.failed] == ["tests::adds"], outcome.result
    assert report.failed[0].file == "src/lib.rs"

    fs = FsTools(runner.env, runner.policy)
    assert (await fs.patch_file("src/lib.rs", [SearchReplace("a - b", "a + b")])).applied
    targeted = adapter.targeted_test(repo, cmds, report.failed[0])
    assert targeted is not None
    again = await runner.run(targeted.argv, timeout=300)
    assert again.result is not None
    rerun = adapter.parse_tests(again.result, cmds)
    assert rerun.ok and rerun.passed == 1, again.result


@cargo_only
async def test_live_compile_error_to_file_line(tmp_path: Path) -> None:
    repo = tmp_path / "calc"
    shutil.copytree(FIXTURES / "rust_calc", repo)
    (repo / "src/lib.rs").write_text('pub fn add(a: i32, b: i32) -> i32 {\n    "x"\n}\n')
    adapter = RustAdapter()
    cmds = adapter.commands(repo, None)
    assert cmds.check is not None
    outcome = await make_runner(repo, tmp_path).run(cmds.check.argv, timeout=300)
    assert outcome.result is not None
    diags = [d for d in adapter.parse_diagnostics(outcome.result, "rustc") if d.severity == "error"]
    assert diags and (diags[0].file, diags[0].line, diags[0].code) == ("src/lib.rs", 2, "E0308")


@pytest.mark.lsp
@pytest.mark.skipif(RustAdapter().lsp_server() is None, reason="rust-analyzer not installed")
@cargo_only
async def test_live_rust_analyzer(tmp_path: Path) -> None:
    repo = tmp_path / "calc"
    shutil.copytree(FIXTURES / "rust_calc", repo)
    spec = RustAdapter().lsp_server()
    assert spec is not None
    env = make_runner(repo, tmp_path).env
    async with LspClient(spec, env, request_timeout=120) as client:
        symbols = await client.document_symbols("src/lib.rs")
        assert "add" in [s.name for s in symbols]
        definition = await client.definition("src/lib.rs", 11, 20)
        assert definition and definition[0].path == "src/lib.rs" and definition[0].line == 1


@cargo_only
async def test_implement_fixes_a_rust_crate(
    home: Path, git_repo: Path, git: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in toolchain_env().items():
        monkeypatch.setenv(name, value)  # `home` swaps HOME; rustup still needs the real toolchains
    shutil.copytree(FIXTURES / "rust_calc", git_repo, dirs_exist_ok=True)
    (git_repo / ".gitignore").write_text("target/\n")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "calc")
    fix = {
        "action": "patch",
        "reason": "wrong operator",
        "path": "src/lib.rs",
        "search": "    a - b\n",  # whole-line block with trailing newline, as models write them
        "replace": "    a + b\n",
    }
    rt = Runtime.create(git_repo, request="fix the failing test", mode="implement")
    result = await implement(
        rt, "fix the failing test", ImplementOptions(coder=ScriptedCoder(fix, DONE), max_steps=4)
    )
    await rt.close(result.outcome.session_status)
    assert result.outcome.status == "completed", result.outcome.report()
    assert "cargo test" in result.outcome.report()
