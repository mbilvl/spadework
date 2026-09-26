import runpy
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from spadework import __version__
from spadework.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"spadework {__version__}\n"


def test_python_dash_m_prints_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["spadework", "--version"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("spadework", run_name="__main__")

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"spadework {__version__}\n"


def test_no_arguments_prints_help() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 2
    assert "Usage: spadework" in result.output
    assert "--version" in result.output


def test_unknown_option_is_a_usage_error() -> None:
    result = runner.invoke(app, ["--no-such-option"])

    assert result.exit_code == 2


def test_importing_the_package_loads_nothing_else() -> None:
    # The sandbox guard imports the package on every tool call, so the
    # package's __init__ must not pull in the CLI or its dependencies.
    code = (
        "import sys; before = set(sys.modules); import spadework; "
        "print(sorted(set(sys.modules) - before))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "['spadework']"
