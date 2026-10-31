import shutil
from pathlib import Path

import pytest

from dfm.processes import ProcessResult
from dfm.tools.languages import SolidityAdapter, TypeScriptAdapter, select_adapter
from dfm.tools.languages.solidity import NO_COMPILER_HINT, remappings
from dfm.tools.results import FailedTest, parse_test_output
from tests import repo_fixtures as fx
from tests.helpers import sandboxed_runner

OUTPUTS = Path(__file__).parent / "fixtures" / "test_outputs"
FORGE = shutil.which("forge")


def result(name: str, code: int = 1) -> ProcessResult:
    return ProcessResult(("forge",), code, (OUTPUTS / name).read_text(), "", 5)


def test_foundry_commands_and_remappings(tmp_path: Path) -> None:
    repo = fx.foundry(tmp_path / "f")
    (repo / "remappings.txt").write_text("forge-std/=lib/forge-std/src/\n")
    (repo / "foundry.toml").write_text(
        "[profile.default]\nsrc = 'src'\nremappings = ['@oz/=lib/openzeppelin/contracts/', 'ctx:solmate/=lib/solmate/src/']\n"
    )
    assert remappings(repo) == {
        "forge-std/": "lib/forge-std/src/",
        "@oz/": "lib/openzeppelin/contracts/",
        "solmate/": "lib/solmate/src/",
    }
    adapter = SolidityAdapter()
    assert isinstance(select_adapter(repo, None), SolidityAdapter)
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None and cmds.test.argv == ("forge", "test")
    assert cmds.check is not None and cmds.check.argv == ("forge", "build")
    assert cmds.format_check is not None and cmds.format_check.argv == ("forge", "fmt", "--check")
    assert cmds.reporter == "forge" and cmds.meta["remap:@oz/"] == "lib/openzeppelin/contracts/"


def test_hardhat_needs_a_local_install(tmp_path: Path) -> None:
    repo = fx.write(tmp_path / "h", {"package.json": "{}", "hardhat.config.ts": ""})
    adapter = SolidityAdapter()
    assert isinstance(select_adapter(repo, None), SolidityAdapter)  # not the TypeScript adapter
    assert not isinstance(select_adapter(repo, None), TypeScriptAdapter)
    bare = adapter.commands(repo, None)
    assert bare.test is None and "local hardhat install" in bare.notes[0]
    fx.write(repo, {"node_modules/.bin/hardhat": "", "pnpm-lock.yaml": ""})
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None and cmds.test.argv == ("pnpm", "exec", "hardhat", "test")
    assert cmds.reporter == "mocha"


def test_targeted_tests() -> None:
    adapter = SolidityAdapter()
    cmds = adapter.commands(Path("/nonexistent"), None)  # no manifest: Hardhat path, no test
    assert adapter.targeted_test(Path(), cmds, FailedTest("x", "y")) is None
    from dfm.tools.languages import ToolchainCommands, ToolCommand

    forge = ToolchainCommands("Solidity", test=ToolCommand(("forge", "test")), reporter="forge")
    t = adapter.targeted_test(Path(), forge, FailedTest("test_add", "x", "test/Calc.t.sol"))
    assert t is not None
    assert t.argv == (
        "forge",
        "test",
        "--match-test",
        "^test_add$",
        "--match-path",
        "test/Calc.t.sol",
    )


def test_goldens() -> None:
    adapter = SolidityAdapter()
    forge = parse_test_output(result("forge_test_SYNTHETIC.txt"), "forge")
    assert [(f.id, f.file, f.message) for f in forge.failed] == [
        ("test_add", "test/Calc.t.sol", "add broken")
    ]
    diags = adapter.parse_diagnostics(result("solc_errors_SYNTHETIC.txt"), "solc")
    assert [(d.severity, d.file, d.line, d.code) for d in diags] == [
        ("error", "src/Counter.sol", 9, "7576"),
        ("warning", "src/Counter.sol", 13, "2072"),
    ]
    mocha = parse_test_output(result("hardhat_mocha_SYNTHETIC.txt"), "mocha")
    assert mocha.passed == 1 and len(mocha.failed) == 1
    failure = mocha.failed[0]
    assert failure.id == "Counter increments"
    assert failure.message == "AssertionError: expected 0 to equal 1"
    assert (failure.file, failure.line) == ("test/Counter.ts", 14)


def test_missing_compiler_is_not_a_test_failure() -> None:
    adapter = SolidityAdapter()
    cmds = adapter.commands(Path(), None)
    real = result("forge_no_compiler.txt")  # captured from forge 1.5.1 on this machine
    report = adapter.parse_tests(real, cmds)
    assert not report.ran and report.errors[0] == NO_COMPILER_HINT
    assert adapter.parse_diagnostics(real, "forge")[0].message == NO_COMPILER_HINT


@pytest.mark.skipif(FORGE is None, reason="forge not installed")
async def test_live_forge_cannot_download_a_compiler_in_the_sandbox(tmp_path: Path) -> None:
    assert FORGE is not None
    repo = fx.foundry(tmp_path / "f")
    runner = sandboxed_runner(repo, tmp_path, str(Path(FORGE).parent))
    adapter = SolidityAdapter()
    cmds = adapter.commands(repo, None)
    assert cmds.check is not None
    outcome = await runner.run(cmds.check.argv, timeout=120)
    assert outcome.result is not None and outcome.result.exit_code != 0
    diags = adapter.parse_diagnostics(outcome.result, "forge")
    # forge would download solc; the sandbox blocks it (unsandboxed, the download succeeded)
    assert not (tmp_path / "home" / ".svm" / "0.8.33").exists()
    assert diags[0].message == NO_COMPILER_HINT, outcome.result
