import json
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


# --- Finding the project, and naming it in messages ----------------------------


def test_commands_find_the_project_from_a_folder_inside_it(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project()
    (root / "out" / "deep").mkdir(parents=True)
    monkeypatch.chdir(root / "out" / "deep")

    result = cli("check")

    assert result.exit_code == 0, result.output
    assert "playbook  fixture 1.0.0" in result.stdout
    # The commands it suggests work from here as they are: no -C.
    assert result.stderr == (
        "See what research sessions will see: spadework check --show-public\n"
    )


def test_errors_found_from_inside_a_project_name_paths_from_here(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(brief="---\ntitle: T\nraeder: R\n---\n")
    (root / "out").mkdir()
    monkeypatch.chdir(root / "out")

    result = cli("check")

    assert result.exit_code == 3
    assert result.stderr == (
        "error: ../brief.md:3: 'reader' is required (is 'raeder' on this line a "
        "misspelling of it?)\n"
        "Fix the front matter of brief.md, then run `spadework check` again.\n"
    )


def test_without_a_project_here_or_above(
    cli: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli("status")

    assert result.exit_code == 3
    assert result.stderr == (
        "error: there is no spadework.toml in this folder or any folder above it\n"
        "Run `spadework init` to create a project, or pass -C PROJECT_FOLDER.\n"
    )


def test_init_never_looks_for_a_project_above(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project()
    (root / "notes").mkdir()
    monkeypatch.chdir(root / "notes")

    result = cli("init", "inner", "--playbook", str(root / "playbooks" / "valid"))

    assert result.exit_code == 0, result.output
    assert (root / "notes" / "inner" / "spadework.toml").is_file()


def test_suggested_commands_carry_the_project_given_with_C(
    cli: Invoke,
    make_project: MakeProject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_project(brief="---\ntitle: T\n---\n")
    monkeypatch.chdir(tmp_path)

    result = cli("check", "-C", "acme-fabrication")
    as_json = json.loads(cli("check", "--json", "-C", root).stdout)

    assert result.exit_code == 3
    assert result.stderr == (
        "error: acme-fabrication/brief.md: 'reader' is required\n"
        "Fix the front matter of brief.md, then run "
        "`spadework check -C acme-fabrication` again.\n"
    )
    # JSON keeps paths relative to the project, and the hint as it is.
    assert as_json["error"]["problems"][0]["path"] == "brief.md"
    assert as_json["error"]["hint"] == (
        "Fix the front matter of brief.md, then run `spadework check` again."
    )


def test_with_project_adds_C_to_each_suggested_command() -> None:
    ui = Ui(project="clients/acme fab")

    assert ui.with_project(
        "Run `spadework run`, then `spadework status --json`. "
        "Or `spadework check -C x`, `spadework init NAME`, `spadework playbooks list`."
    ) == (
        "Run `spadework run -C 'clients/acme fab'`, then "
        "`spadework status -C 'clients/acme fab' --json`. Or `spadework check -C x`, "
        "`spadework init NAME`, `spadework playbooks list`."
    )
    assert Ui().with_project("`spadework run`") == "`spadework run`"


@pytest.mark.parametrize("command", ["check", "run", "status", "verify", "doctor"])
def test_a_project_given_as_an_argument_gets_a_hint(
    cli: Invoke,
    make_project: MakeProject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    make_project()
    monkeypatch.chdir(tmp_path)

    result = cli(command, "acme-fabrication")

    assert result.exit_code == 2
    assert result.stderr == (
        f"error: {command} takes no arguments, but was given 'acme-fabrication'\n"
        f"Did you mean `spadework {command} -C acme-fabrication`?\n"
    )


def test_an_argument_that_is_not_a_folder(cli: Invoke) -> None:
    result = cli("status", "nope")

    assert result.exit_code == 2
    assert result.stderr == (
        "error: status takes no arguments, but was given 'nope'\n"
        "See `spadework status --help`.\n"
    )


@pytest.mark.parametrize("command", ["status", "run", "verify"])
def test_inputs_that_are_not_text_are_named_by_every_command(
    cli: Invoke,
    make_project: MakeProject,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "fake")
    monkeypatch.delenv("SPADEWORK_FAKE_SCRIPT", raising=False)
    root = make_project(inputs={"scan.pdf": b"%PDF-1.7"})

    result = cli(command, "-C", root)

    assert result.exit_code == 0, result.output
    assert result.stderr.startswith(
        "warning: inputs/scan.pdf is ignored: only .md and .txt inputs are read\n"
        "Convert it to Markdown or plain text to use it, or move it out of inputs/.\n"
    )
