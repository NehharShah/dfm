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


def test_bare_invocation_without_a_terminal_lists_the_commands() -> None:
    result = runner.invoke(app, [])  # CliRunner is not a terminal: no interactive session
    assert result.exit_code == 0
    assert "needs a terminal" in result.stdout
    assert "implement" in result.stdout and "chat" in result.stdout
