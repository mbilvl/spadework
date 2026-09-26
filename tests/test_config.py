import tomllib
from pathlib import Path

import pytest

from spadework.config import ProjectConfig, ProjectSection, load_config, render_config
from spadework.errors import ExitCode, InvalidError

MINIMAL = """\
schema_version = 1

[project]
name = "acme-fabrication"
playbook = "quickstart"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spadework.toml"
    path.write_text(text, encoding="utf-8")
    return path


def problems(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(InvalidError) as caught:
        load_config(write(tmp_path, text))
    assert caught.value.exit_code == ExitCode.INVALID
    assert caught.value.hint == "Fix spadework.toml, then run `spadework check` again."
    return [str(problem) for problem in caught.value.problems]


def test_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL))

    assert config.project.name == "acme-fabrication"
    assert config.project.playbook == "quickstart"
    assert (config.models.draft, config.models.review) == ("sonnet", "opus")
    assert config.limits.budget_usd == 6.00
    assert (config.limits.max_time, config.limits.max_time_s) == ("2h", 7200)
    assert config.limits.rounds == 2
    assert (config.limits.session_timeout, config.limits.session_timeout_s) == (
        "25m",
        1500,
    )
    assert config.executor.name == "claude-cli"
    assert config.executor.bin is None
    assert config.verify.enabled is True


def test_the_spec_example_is_valid(tmp_path: Path) -> None:
    text = MINIMAL + (
        '\n[models]\ndraft = "sonnet"\nreview = "opus"\n'
        '\n[limits]\nbudget_usd = 6.00\nmax_time = "90m"\nrounds = 2\n'
        'session_timeout = "25m"\n'
        '\n[executor]\nname = "fake"\nbin = "/opt/claude"\n'
        "\n[verify]\nenabled = false\n"
    )
    config = load_config(write(tmp_path, text))

    assert config.limits.max_time_s == 5400
    assert config.executor.name == "fake"
    assert config.verify.enabled is False


def test_an_integer_budget_is_accepted(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL + "\n[limits]\nbudget_usd = 3\n"))

    assert config.limits.budget_usd == 3.0


def test_init_writes_the_model_defaults() -> None:
    written = ProjectConfig.model_validate(
        tomllib.loads(render_config("demo", "quickstart"))
    )

    expected = ProjectConfig(
        schema_version=1, project=ProjectSection(name="demo", playbook="quickstart")
    )
    assert written == expected


def test_init_escapes_strings() -> None:
    text = render_config("demo", 'odd "path"\\with\ttab\x01')

    assert tomllib.loads(text)["project"]["playbook"] == 'odd "path"\\with\ttab\x01'


def test_unknown_keys_are_errors_with_a_suggestion(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + '\n[limits]\nbudget_usd = 5\nmax_tme = "1h"\n\n[executr]\nname = "x"\n'
    )

    assert problems(tmp_path, text) == [
        "spadework.toml:9: unknown key 'limits.max_tme' (did you mean 'max_time'?)",
        "spadework.toml:11: unknown key 'executr' (did you mean 'executor'?)",
    ]


def test_missing_sections_and_keys(tmp_path: Path) -> None:
    assert problems(tmp_path, "") == [
        "spadework.toml: 'schema_version' is required",
        "spadework.toml: 'project' is required",
    ]
    assert problems(tmp_path, 'schema_version = 1\n[project]\nname = "x"\n') == [
        "spadework.toml:2: 'project.playbook' is required"
    ]


def test_unsupported_schema_version(tmp_path: Path) -> None:
    (message,) = problems(
        tmp_path, MINIMAL.replace("schema_version = 1", "schema_version = 2")
    )

    assert message == (
        "spadework.toml:1: schema_version: schema_version 2 is not supported; "
        "this version of spadework reads schema_version 1"
    )


@pytest.mark.parametrize(
    ("addition", "expected"),
    [
        ("\n[limits]\nbudget_usd = 0\n", "limits.budget_usd: should be greater than 0"),
        (
            '\n[limits]\nbudget_usd = "6"\n',
            "limits.budget_usd: should be a valid number",
        ),
        (
            '\n[limits]\nmax_time = "2 hours"\n',
            "limits.max_time: '2 hours' is not a duration",
        ),
        ('\n[limits]\nsession_timeout = "0s"\n', "not a duration longer than zero"),
        (
            "\n[limits]\nrounds = -1\n",
            "limits.rounds: should be greater than or equal to 0",
        ),
        (
            '\n[executor]\nname = "codex"\n',
            "executor.name: should be 'claude-cli' or 'fake'",
        ),
        ('\n[verify]\nenabled = "yes"\n', "verify.enabled: should be a valid boolean"),
        ('\n[models]\ndraft = ""\n', "models.draft: must not be empty"),
        ("\n[models]\ndraft = 3\n", "models.draft: should be a valid string"),
    ],
)
def test_invalid_values(tmp_path: Path, addition: str, expected: str) -> None:
    (message,) = problems(tmp_path, MINIMAL + addition)

    assert expected in message
    assert message.startswith("spadework.toml:")


def test_a_section_that_is_not_a_table(tmp_path: Path) -> None:
    assert problems(tmp_path, "limits = 3\n" + MINIMAL) == [
        "spadework.toml:1: limits: should be a table of keys and values"
    ]


def test_invalid_project_name(tmp_path: Path) -> None:
    (message,) = problems(
        tmp_path, MINIMAL.replace("acme-fabrication", "Acme Fabrication")
    )

    assert message == (
        "spadework.toml:4: project.name: 'Acme Fabrication' is not valid: use "
        "lowercase letters, digits and '-', such as 'acme-fabrication'"
    )


def test_invalid_toml(tmp_path: Path) -> None:
    (message,) = problems(tmp_path, "schema_version = \n")

    assert message.startswith("spadework.toml:1: invalid TOML: ")
