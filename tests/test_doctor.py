import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.config import load_config
from dfm.doctor import CheckResult, Probe, Status, exit_code, run_checks


def fake_probe(tools: set[str], modules: set[str], python: tuple[int, int] = (3, 12)) -> Probe:
    outputs: dict[tuple[str, ...], tuple[int, str]] = {
        ("sysctl", "-n", "hw.memsize"): (0, str(48 * 1024**3)),
        ("sysctl", "-n", "machdep.cpu.brand_string"): (0, "Apple M5 Pro"),
        ("fm", "available"): (0, "System model available"),
    }

    def run(argv: Sequence[str]) -> tuple[int, str]:
        return outputs.get(tuple(argv), (0, f"{argv[0]} 1.0"))

    return Probe(
        which=lambda name: f"/bin/{name}" if name in tools else None,
        run=run,
        find_module=lambda name: name in modules,
        system=lambda: "Darwin",
        machine=lambda: "arm64",
        mac_version=lambda: "27.0.1",
        python_version=python,
        disk_free=lambda _: 500 * 1024**3,
        home=Path("/"),
    )


def by_name(results: list[CheckResult], name: str) -> CheckResult:
    return next(r for r in results if r.name == name)


def test_healthy_machine(tmp_path: Path) -> None:
    probe = fake_probe(
        {"git", "fm", "uv", "reflex", "rg", "gh"}, {"mlx", "mlx_lm", "repocanon", "repokg"}
    )
    results = run_checks(probe, load_config(None, env={"HOME": str(tmp_path)}))
    assert by_name(results, "Apple FM").status is Status.OK
    assert by_name(results, "memory").detail == "48 GB unified memory"
    assert by_name(results, "Apple Silicon").detail == "Apple M5 Pro"
    assert by_name(results, "remote inference").detail.startswith("DENIED")
    assert by_name(results, "RepoCanon").status is Status.OK
    assert by_name(results, "dfm-primary").status is Status.WARN  # not configured yet
    assert exit_code(results) == 0


def test_missing_optional_pieces_only_warn(tmp_path: Path) -> None:
    results = run_checks(fake_probe({"git"}, set()), load_config(None, env={"HOME": str(tmp_path)}))
    for name in ("fm CLI", "MLX", "RepoCanon", "RepoKG", "Reflex", "gh", "rg", "docker"):
        assert by_name(results, name).status is Status.WARN, name
    assert exit_code(results) == 0


def test_blocking_failures(tmp_path: Path) -> None:
    results = run_checks(
        fake_probe(set(), set(), python=(3, 11)), load_config(None, env={"HOME": str(tmp_path)})
    )
    assert by_name(results, "git").status is Status.FAIL
    assert by_name(results, "Python").status is Status.FAIL
    assert exit_code(results) == 1


def test_missing_home_is_a_skip_not_a_traceback(tmp_path: Path) -> None:
    import dataclasses

    def no_home(_: Path) -> int:
        raise FileNotFoundError(2, "No such file or directory", "/tmp/nonexistent")

    probe = dataclasses.replace(fake_probe({"git"}, set()), disk_free=no_home)
    results = run_checks(probe, load_config(None, env={"HOME": str(tmp_path)}))
    disk = by_name(results, "disk")
    assert disk.status is Status.SKIP and "cannot measure" in disk.detail


def test_remote_inference_warns(tmp_path: Path) -> None:
    cfg = tmp_path / ".config" / "dfm"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text('[network]\nremote_inference = "ask"\n')
    results = run_checks(fake_probe({"git"}, set()), load_config(None, env={"HOME": str(tmp_path)}))
    assert by_name(results, "remote inference").status is Status.WARN


def test_cli_json_runs_on_this_machine(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["doctor", "--json"], env={"HOME": str(tmp_path)})
    payload = json.loads(result.stdout)
    names = {item["name"] for item in payload}
    assert {"git", "Python", "remote inference", "Apple FM", "fm CLI"} & names
    assert result.exit_code in (0, 1)


def test_toolchain_versions_and_proxy_lsp(tmp_path: Path) -> None:
    probe = fake_probe({"git", "cargo", "rustfmt", "rust-analyzer"}, set())
    real_run = probe.run
    probe.run = lambda argv: (
        (1, "error: Unknown binary") if argv[0] == "rust-analyzer" else real_run(argv)
    )
    results = run_checks(probe, load_config(None, env={"HOME": str(tmp_path)}))
    assert by_name(results, "clippy").status is Status.OK  # `cargo clippy`, found via cargo
    assert by_name(results, "rustfmt").detail == "rustfmt 1.0"
    analyzer = by_name(results, "rust-analyzer")
    assert analyzer.status is Status.WARN and "rustup component add" in analyzer.remedy


def test_table_prints_remedies_with_their_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    hint = "uv tool install 'dfm-agent[mlx]'"
    monkeypatch.setattr(
        "dfm.cli.doctor_cmd.run_checks",
        lambda loaded=None: [CheckResult("models", "mlx-lm", Status.WARN, "not installed", hint)],
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert hint in " ".join(result.stdout.split())
