import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spadework import __version__
from spadework.cli import app
from spadework.console import Ui
from tests.conftest import Invoke, MakeProject

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


def test_the_cli_module_does_not_import_heavy_dependencies() -> None:
    # --help and --version must stay fast.
    code = (
        "import sys; import spadework.cli; "
        "print(sorted({m.split('.')[0] for m in sys.modules} & "
        "{'pydantic', 'jinja2', 'yaml', 'rich'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]"


def test_global_options_work_before_and_after_the_command(
    cli: Invoke, make_project: MakeProject
) -> None:
    root = make_project()

    before = cli("-C", root, "-q", "check")
    after = cli("check", "-C", root, "-q")

    assert before.exit_code == after.exit_code == 0
    assert before.stdout == after.stdout
    assert "playbook  fixture 1.0.0" in before.stdout


def test_project_must_be_an_existing_folder(cli: Invoke, tmp_path: Path) -> None:
    result = cli("check", "-C", tmp_path / "nope")

    assert result.exit_code == 2
    assert "does not exist" in result.stderr


def test_quiet_and_verbose_together_is_a_usage_error(
    cli: Invoke, tmp_path: Path
) -> None:
    result = cli("-q", "check", "-v", "-C", tmp_path)

    assert result.exit_code == 2
    assert (
        result.stderr
        == "error: -q and -v cannot be used together\nChoose one of them.\n"
    )


def test_errors_are_a_line_and_a_hint_without_traceback(
    cli: Invoke, tmp_path: Path
) -> None:
    result = cli("check", "-C", tmp_path)

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr == (
        f"error: there is no spadework.toml in {tmp_path}\n"
        "Run `spadework init` to create a project, or pass -C PROJECT_FOLDER.\n"
    )


@pytest.mark.parametrize(
    ("args", "env"), [(["-v"], {}), ([], {"SPADEWORK_DEBUG": "1"})]
)
def test_verbose_or_debug_shows_tracebacks(
    cli: Invoke, tmp_path: Path, args: list[str], env: dict[str, str]
) -> None:
    result = cli(*args, "check", "-C", tmp_path, env=env)

    assert result.exit_code == 3
    assert "Traceback (most recent call last)" in result.stderr
    assert "error: there is no spadework.toml" in result.stderr


def test_unexpected_errors_exit_1_with_a_hint(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(root: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("spadework.project.load_project", boom)

    result = cli("check", "-C", make_project())

    assert result.exit_code == 1
    assert result.stderr == (
        "error: unexpected error: RuntimeError: boom\n"
        "Run again with -v to see the traceback. Please report it as a bug.\n"
    )


def test_unexpected_errors_propagate_with_verbose(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(root: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("spadework.project.load_project", boom)

    result = cli("check", "-v", "-C", make_project())

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)


def test_interrupts_exit_130(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(root: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("spadework.project.load_project", interrupt)

    result = cli("check", "-C", make_project())

    assert result.exit_code == 130
    assert result.stderr == "interrupted\n"


def test_colour_is_off_with_no_color(cli: Invoke, tmp_path: Path) -> None:
    coloured = cli("check", "-C", tmp_path, env={"FORCE_COLOR": "1"})
    plain = cli("check", "--no-color", "-C", tmp_path, env={"FORCE_COLOR": "1"})
    by_env = cli("check", "-C", tmp_path, env={"FORCE_COLOR": "1", "NO_COLOR": "1"})

    assert "\x1b[1;31m" in coloured.stderr
    assert "31m" not in plain.stderr
    assert "31m" not in by_env.stderr


def test_ui_colour_setting() -> None:
    assert Ui(color=False).out.no_color is True
    assert Ui().err.stderr is True


def test_exits_raised_by_commands_pass_through(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    import typer

    def stop(root: Path) -> None:
        raise typer.Exit(4)

    monkeypatch.setattr("spadework.project.load_project", stop)

    result = cli("check", "-C", make_project())

    assert (result.exit_code, result.stderr) == (4, "")
