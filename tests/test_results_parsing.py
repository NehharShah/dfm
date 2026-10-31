import json
from pathlib import Path

import pytest

from dfm.processes import ProcessResult
from dfm.tools.results import (
    compress,
    compress_diagnostics,
    detect_reporter,
    parse_cargo_json,
    parse_diagnostics_text,
    parse_jest_json,
    parse_junit_xml,
    parse_pyright_json,
    parse_ruff_json,
    parse_tap,
    parse_test_output,
)

FIX = Path(__file__).parent / "fixtures" / "test_outputs"


def result(name: str, exit_code: int) -> ProcessResult:
    return ProcessResult(("x",), exit_code, (FIX / name).read_text(), "", 10)


# Golden outputs captured from the real tools on this machine (paths anonymized).


def test_pytest_real() -> None:
    report = parse_test_output(result("pytest_fail.txt", 1))
    assert report.framework == "pytest" and report.ran
    assert (report.passed, len(report.failed)) == (1, 1)
    failure = report.failed[0]
    assert failure.id == "test_calc.py::test_add"
    assert failure.message == "assert -1 == 5"
    assert (failure.file, failure.line) == ("test_calc.py", 7)
    assert not report.ok


def test_pytest_collection_error_is_not_a_test_failure() -> None:
    report = parse_test_output(result("pytest_collection_error.txt", 2))
    assert not report.ok
    assert report.errors
    assert not report.failed


def test_unittest_real() -> None:
    report = parse_test_output(result("unittest_fail.txt", 1))
    assert report.framework == "unittest"
    assert (report.passed, len(report.failed)) == (1, 1)
    assert report.failed[0].id.endswith("test_add")
    assert "AssertionError" in report.failed[0].message


def test_go_test_json_real() -> None:
    report = parse_test_output(result("go_test.json", 1))
    assert report.framework == "go test"
    assert (report.passed, len(report.failed)) == (1, 1)
    failure = report.failed[0]
    assert failure.id.endswith("TestAdd")
    assert failure.file == "calc_test.go" and failure.line is not None
    assert "want 5" in failure.message


def test_cargo_test_real() -> None:
    report = parse_test_output(result("cargo_test.txt", 101))
    assert report.framework == "cargo test"
    assert (report.passed, len(report.failed)) == (1, 1)
    failure = report.failed[0]
    assert failure.id == "tests::adds"
    assert (failure.file, failure.line) == ("src/lib.rs", 9)
    assert "left == right" in failure.message


def test_forge_synthetic_format() -> None:
    report = parse_test_output(result("forge_test_SYNTHETIC.txt", 1))
    assert report.framework == "forge test"
    assert (report.passed, len(report.failed)) == (1, 1)
    assert report.failed[0].id == "test_add" and report.failed[0].message == "add broken"


def test_forge_without_compiler_did_not_run() -> None:
    report = parse_test_output(result("forge_no_compiler.txt", 1), "forge")
    assert not report.ran
    assert "no compiler versions" in report.errors[0]
    assert "did not run tests" in report.summary()


def test_command_not_found_and_timeout() -> None:
    missing = parse_test_output(
        ProcessResult(("pytest",), 127, "", "sh: pytest: command not found", 3)
    )
    assert not missing.ran and "not found" in missing.errors[0]
    slow = parse_test_output(ProcessResult(("pytest",), None, "", "", 5000, timed_out=True))
    assert not slow.ran and "timed out" in slow.errors[0]


def test_jest_json() -> None:
    data = {
        "numTotalTests": 3,
        "numPassedTests": 1,
        "numPendingTests": 1,
        "testResults": [
            {
                "name": "/repo/src/sum.test.ts",
                "status": "failed",
                "assertionResults": [
                    {
                        "fullName": "sum adds",
                        "status": "failed",
                        "failureMessages": ["Error: expected 5, got -1\n    at sum.test.ts:4:10"],
                    },
                    {"fullName": "sum ok", "status": "passed", "failureMessages": []},
                ],
            }
        ],
    }
    report = parse_jest_json("noise before\n" + json.dumps(data))
    assert (report.passed, report.skipped, len(report.failed)) == (1, 1, 1)
    assert report.failed[0].message == "Error: expected 5, got -1"
    assert detect_reporter(json.dumps(data)) == "jest-json"


def test_tap_and_junit() -> None:
    tap = parse_tap("TAP version 13\nok 1 - a\nnot ok 2 - b\nok 3 - c # SKIP later\n1..3\n")
    assert (tap.passed, len(tap.failed), tap.skipped) == (1, 1, 1)
    xml = (
        '<?xml version="1.0"?><testsuite><testcase classname="m.T" name="a" time="0.1"/>'
        '<testcase classname="m.T" name="b" time="0.2" file="t.py" line="9">'
        '<failure message="boom">trace</failure></testcase>'
        '<testcase name="c"><skipped/></testcase></testsuite>'
    )
    junit = parse_junit_xml(xml)
    assert (junit.passed, len(junit.failed), junit.skipped) == (1, 1, 1)
    assert junit.failed[0].id == "m.T.b" and junit.failed[0].line == 9
    assert parse_junit_xml("<oops").ran is False


def test_diagnostics_real_cargo_pyright_ruff() -> None:
    cargo = parse_cargo_json((FIX / "cargo_check.json").read_text())
    assert cargo and cargo[0].severity == "error"
    assert cargo[0].file == "src/lib.rs" and cargo[0].line == 1
    pyright = parse_pyright_json((FIX / "pyright.json").read_text())
    assert pyright[0].severity == "error" and pyright[0].line == 2
    ruff = parse_ruff_json((FIX / "ruff.json").read_text())
    assert {d.code for d in ruff} >= {"E402"}


def test_text_diagnostics() -> None:
    text = (
        "src/a.ts(3,7): error TS2322: Type 'number' is not assignable to type 'string'.\n"
        "main.c:10:5: warning: unused variable 'x' [-Wunused-variable]\n"
        "pkg/x.go:4:2: error: undefined: foo\n"
    )
    diags = parse_diagnostics_text(text)
    assert [(d.file, d.line, d.severity) for d in diags] == [
        ("src/a.ts", 3, "error"),
        ("main.c", 10, "warning"),
        ("pkg/x.go", 4, "error"),
    ]
    assert diags[0].code == "TS2322"


def test_compress_dedupes_and_caps() -> None:
    report = parse_test_output(result("cargo_test.txt", 101))
    report.failed = report.failed * 3
    text = compress(report, repo_markers=("src/",))
    assert text.startswith("cargo test: 1 passed, 3 failed")
    assert text.count("same failure as above") == 2
    diags = parse_diagnostics_text("a.py:1:1: error: x\n" * 40)
    assert "more" in compress_diagnostics(diags, limit=10)


@pytest.mark.parametrize(
    ("name", "reporter"),
    [
        ("pytest_fail.txt", "pytest"),
        ("unittest_fail.txt", "unittest"),
        ("go_test.json", "go-json"),
        ("cargo_test.txt", "cargo"),
        ("forge_test_SYNTHETIC.txt", "forge"),
    ],
)
def test_detect_reporter(name: str, reporter: str) -> None:
    assert detect_reporter((FIX / name).read_text()) == reporter
