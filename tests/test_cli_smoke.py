from typer.testing import CliRunner

from dfm import __version__
from dfm.cli.app import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"dfm {__version__}"


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Developer Foundation Model" in result.stdout


def test_bare_invocation_is_honest_about_missing_session() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "not built yet" in result.stdout
