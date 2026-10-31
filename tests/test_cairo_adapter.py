from pathlib import Path

from dfm.processes import ProcessResult
from dfm.tools.languages import CairoAdapter, select_adapter
from dfm.tools.languages.cairo import package_dir, workspace_members
from dfm.tools.results import FailedTest, detect_reporter, parse_test_output
from tests import repo_fixtures as fx

OUTPUTS = Path(__file__).parent / "fixtures" / "test_outputs"


def result(name: str, code: int = 1) -> ProcessResult:
    return ProcessResult(("scarb",), code, (OUTPUTS / name).read_text(), "", 5)


def starknet(root: Path) -> Path:
    return fx.write(
        root,
        {
            "Scarb.toml": '[workspace]\nmembers = ["packages/*"]\n',
            "package.json": "{}",
            "packages/token/Scarb.toml": '[package]\nname = "token"\n\n[dev-dependencies]\nsnforge_std = "0.40.0"\n',
            "packages/token/src/lib.cairo": "",
            "packages/math/Scarb.toml": '[package]\nname = "math"\n',
            "packages/math/src/lib.cairo": "",
        },
    )


def test_workspace_and_runner_choice(tmp_path: Path) -> None:
    repo = starknet(tmp_path / "s")
    adapter = CairoAdapter()
    assert isinstance(select_adapter(repo, None), CairoAdapter)  # not TypeScript
    assert workspace_members(repo) == {"math": "packages/math", "token": "packages/token"}
    assert package_dir(repo, "packages/token/src/lib.cairo") == "packages/token"
    snforge = adapter.commands(repo, None, "packages/token/src/lib.cairo")
    assert snforge.test is not None and snforge.test.argv == ("snforge", "test")
    assert snforge.test.cwd == "packages/token"
    plain = adapter.commands(repo, None, "packages/math/src/lib.cairo")
    assert plain.test is not None and plain.test.argv == ("scarb", "test")
    assert plain.check is not None and plain.check.argv == ("scarb", "build")
    assert plain.format_check is not None and plain.format_check.argv[-1] == "--check"
    assert any("not installed" in n for n in snforge.notes) == (adapter.tools()["snforge"] is None)


def test_targeted_snforge_only() -> None:
    adapter = CairoAdapter()
    from dfm.tools.languages import ToolchainCommands, ToolCommand

    sn = ToolchainCommands("Cairo", test=ToolCommand(("snforge", "test")), reporter="cairo")
    t = adapter.targeted_test(Path(), sn, FailedTest("calc_tests::test_add", "x"))
    assert t is not None and t.argv == ("snforge", "test", "calc_tests::test_add", "--exact")
    scarb = ToolchainCommands("Cairo", test=ToolCommand(("scarb", "test")), reporter="cairo")
    assert adapter.targeted_test(Path(), scarb, FailedTest("x", "y")) is None


def test_synthetic_outputs() -> None:
    sn = parse_test_output(result("snforge_test_SYNTHETIC.txt"), "cairo")
    assert sn.framework == "snforge" and sn.passed == 1
    assert [(f.id, f.message) for f in sn.failed] == [
        ("calc_tests::test_add", "0x6164642062726f6b656e ('add broken')")
    ]
    ct = parse_test_output(result("scarb_cairo_test_SYNTHETIC.txt"), "cairo")
    assert ct.passed == 1 and [f.id for f in ct.failed] == ["calc::tests::test_add"]
    assert "add broken" in ct.failed[0].message
    diags = CairoAdapter().parse_diagnostics(result("scarb_build_error_SYNTHETIC.txt"), "scarb")
    assert [(d.file, d.line, d.column) for d in diags] == [("/work/calc/src/lib.cairo", 2, 5)]
    for name in ("scarb_cairo_test_SYNTHETIC.txt", "snforge_test_SYNTHETIC.txt"):
        assert detect_reporter((OUTPUTS / name).read_text()) == "cairo"
    assert detect_reporter((OUTPUTS / "cargo_test.txt").read_text()) == "cargo"


def test_build_failure_is_not_a_test_result() -> None:
    adapter = CairoAdapter()
    cmds = adapter.commands(Path(), None)
    report = adapter.parse_tests(result("scarb_build_error_SYNTHETIC.txt"), cmds)
    assert not report.ran
    assert any("/work/calc/src/lib.cairo:2:5: Unexpected return type" in e for e in report.errors)
