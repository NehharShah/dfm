import shutil
from pathlib import Path

import pytest

from dfm.processes import ProcessResult
from dfm.tools.filesystem import FsTools, SearchReplace
from dfm.tools.languages import SwiftAdapter, select_adapter
from dfm.tools.languages.swift import MACRO_PLUGIN_FLAKE, swift_testing_plugin_dir
from dfm.tools.results import FailedTest, TestReport, detect_reporter
from dfm.tools.shell.classify import classify
from dfm.tools.shell.runner import CommandRunner
from tests import repo_fixtures as fx
from tests.helpers import sandboxed_runner

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUTS = FIXTURES / "test_outputs"
SWIFT = shutil.which("swift")
swift_only = pytest.mark.skipif(SWIFT is None, reason="swift not installed")


def result(stdout: str, code: int = 1, stderr: str = "") -> ProcessResult:
    return ProcessResult(("swift",), code, stdout, stderr, 5)


def test_commands_and_detection(tmp_path: Path) -> None:
    repo = FIXTURES / "swift_calc"
    adapter = SwiftAdapter()
    assert isinstance(select_adapter(repo, None), SwiftAdapter)
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None and cmds.test.argv[:3] == ("swift", "test", "--disable-sandbox")
    plugins = swift_testing_plugin_dir()
    if plugins is not None:  # works around the intermittent macro plugin lookup failure
        assert cmds.test.argv[3:] == ("-Xswiftc", "-plugin-path", "-Xswiftc", plugins)
    assert cmds.check is not None
    assert cmds.check.argv == ("swift", "build", "--disable-sandbox")  # DFM's sandbox is outside
    assert cmds.reporter == "swift" and cmds.format is None
    nested = fx.write(
        tmp_path / "app",
        {
            "Packages/Core/Package.swift": "",
            "Packages/Core/Sources/Core/A.swift": "",
            ".swift-format": "",
        },
    )
    assert adapter.commands(nested, None, "Packages/Core/Sources/Core/A.swift").test is not None
    xcode = fx.write(tmp_path / "x", {"App.xcodeproj/project.pbxproj": "", "App/A.swift": ""})
    only_xcode = adapter.commands(xcode, None)
    assert only_xcode.test is None and "xcodebuild is not run" in only_xcode.notes[0]


def test_swift_format_classification() -> None:
    assert (
        classify("swift format lint --strict --recursive .").parts[0].action.value == "SAFE_BUILD"
    )
    assert (
        classify("swift format --in-place --recursive .").parts[0].action.value == "WORKTREE_WRITE"
    )


def test_targeted_filters() -> None:
    adapter = SwiftAdapter()
    cmds = adapter.commands(FIXTURES / "swift_calc", None)
    t = adapter.targeted_test(FIXTURES, cmds, FailedTest("addsTwoNumbers", "x"))
    assert t is not None and t.argv[:2] == ("swift", "test")
    assert t.argv[-2:] == ("--filter", "addsTwoNumbers")
    x = adapter.targeted_test(FIXTURES, cmds, FailedTest("CalcTests.CalcTests/testAdd", "x"))
    assert x is not None and x.argv[-1] == r"CalcTests\.CalcTests/testAdd"


def test_goldens() -> None:
    adapter = SwiftAdapter()
    cmds = adapter.commands(FIXTURES / "swift_calc", None)
    text = (OUTPUTS / "swift_testing_fail.txt").read_text()
    assert detect_reporter(text) == "swift"
    report = adapter.parse_tests(result(text), cmds)
    assert report.passed == 1 and [f.id for f in report.failed] == ["addsTwoNumbers"]
    assert (report.failed[0].file, report.failed[0].line) == ("CalcTests.swift", 5)
    localized = report.localize(FIXTURES / "swift_calc")  # base name → its unique match
    assert localized.failed[0].file == "Tests/CalcTests/CalcTests.swift"
    xc = adapter.parse_tests(result((OUTPUTS / "swift_xctest_SYNTHETIC.txt").read_text()), cmds)
    assert xc.passed == 1 and [f.id for f in xc.failed] == ["CalcTests.CalcTests/testAdd"]
    assert xc.failed[0].line == 6 and "XCTAssertEqual failed" in xc.failed[0].message
    build = (OUTPUTS / "swift_build_error.txt").read_text()
    diags = adapter.parse_diagnostics(result("", 1, build), "swiftc")
    assert [(d.file, d.line, d.column) for d in diags] == [
        ("/work/sw/Sources/Calc/Calc.swift", 2, 5)
    ]
    assert "\x1b" not in diags[0].message


def test_basename_resolution_refuses_ambiguity(tmp_path: Path) -> None:
    repo = fx.write(tmp_path / "r", {"A/x.swift": "", "B/x.swift": ""})
    report = TestReport("swift test", failed=[FailedTest("t", "m", "x.swift", 1)])
    assert report.localize(repo).failed[0].file == "x.swift"


# ---------------------------------------------------------------- live: real swift through the sandbox


def make_runner(repo: Path, tmp_path: Path) -> CommandRunner:
    assert SWIFT is not None
    return sandboxed_runner(repo, tmp_path, str(Path(SWIFT).parent))


async def run_swift(runner: CommandRunner, argv: tuple[str, ...]) -> ProcessResult:
    """Retry only the known toolchain flake (see MACRO_PLUGIN_FLAKE); anything else is real."""
    for _ in range(3):
        outcome = await runner.run(argv, timeout=900)
        assert outcome.result is not None
        if MACRO_PLUGIN_FLAKE not in outcome.result.stderr:
            return outcome.result
    pytest.skip("Swift Testing macro plugin failed to load three times (toolchain flake)")


def test_toolchain_flake_is_reported_as_transient() -> None:
    adapter = SwiftAdapter()
    cmds = adapter.commands(FIXTURES / "swift_calc", None)
    stderr = f"CalcTests.swift:4:12: error: external macro ...; {MACRO_PLUGIN_FLAKE}\n"
    report = adapter.parse_tests(result("", 1, stderr), cmds)
    assert not report.ran and report.errors[0].startswith("toolchain:")


@swift_only
async def test_live_fail_fix_targeted_pass_and_build_error(tmp_path: Path) -> None:
    repo = tmp_path / "calc"
    shutil.copytree(FIXTURES / "swift_calc", repo)
    adapter = SwiftAdapter()
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None
    runner = make_runner(repo, tmp_path)
    first = await run_swift(runner, cmds.test.argv)
    report = adapter.parse_tests(first, cmds).localize(repo)
    assert [f.id for f in report.failed] == ["addsTwoNumbers"], first
    assert (report.failed[0].file, report.failed[0].line) == ("Tests/CalcTests/CalcTests.swift", 5)

    fs = FsTools(runner.env, runner.policy)
    assert (
        await fs.patch_file(
            "Sources/Calc/Calc.swift", [SearchReplace("    a - b\n", "    a + b\n")]
        )
    ).applied
    targeted = adapter.targeted_test(repo, cmds, report.failed[0])
    assert targeted is not None
    again = await run_swift(runner, targeted.argv)
    rerun = adapter.parse_tests(again, cmds)
    assert rerun.ok and rerun.passed == 1, again

    (repo / "Sources/Calc/Calc.swift").write_text(
        'public func add(_ a: Int, _ b: Int) -> Int {\n    "x"\n}\n'
    )
    assert cmds.check is not None
    build = await runner.run(cmds.check.argv, timeout=900)
    assert build.result is not None
    errors = [d for d in adapter.parse_diagnostics(build.result, "swiftc") if d.severity == "error"]
    assert errors and errors[0].line == 2
    assert errors[0].file is not None and errors[0].file.endswith("Sources/Calc/Calc.swift")
