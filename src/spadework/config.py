"""``spadework.toml``: the project's configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from spadework.durations import parse_duration
from spadework.models import (
    Duration,
    NonEmptyStr,
    SchemaVersion,
    Slug,
    StrictModel,
    Usd,
    raise_for,
)
from spadework.textfiles import load_toml

CONFIG_FILE = "spadework.toml"
CONFIG_HINT = f"Fix {CONFIG_FILE}, then run `spadework check` again."


class ProjectSection(StrictModel):
    """Which project this is and which playbook it runs."""

    name: Slug = Field(description="Slug used in logs and the run manifest.")
    playbook: NonEmptyStr = Field(
        description=(
            "A built-in playbook name such as 'discovery-website', or a path "
            "to a playbook directory relative to the project, such as "
            "'./playbooks/mine'."
        )
    )


class ModelsSection(StrictModel):
    """Maps the tiers that tasks declare to models."""

    draft: NonEmptyStr = Field(
        default="sonnet", description="Model for the draft tier."
    )
    review: NonEmptyStr = Field(
        default="opus", description="Model for the review tier."
    )


class LimitsSection(StrictModel):
    """Money and time limits. A run never goes past them."""

    budget_usd: Usd = Field(
        default=6.00, description="Spending cap per run, in US dollars."
    )
    max_time: Duration = Field(
        default="2h", description="Wall-clock cap per run, such as '90m' or '2h'."
    )
    rounds: int = Field(default=2, ge=0, description="Maximum number of review rounds.")
    session_timeout: Duration = Field(
        default="25m", description="Wall-clock cap for any one session."
    )

    @property
    def max_time_s(self) -> int:
        return parse_duration(self.max_time)

    @property
    def session_timeout_s(self) -> int:
        return parse_duration(self.session_timeout)


class ExecutorSection(StrictModel):
    """The adapter that runs sessions."""

    name: Literal["claude-cli", "fake"] = Field(
        default="claude-cli",
        description="'claude-cli' runs `claude -p`. 'fake' is for tests and demos.",
    )
    bin: NonEmptyStr | None = Field(
        default=None,
        description=(
            "Path to the claude binary. Default: SPADEWORK_CLAUDE_BIN, then PATH."
        ),
    )


class VerifySection(StrictModel):
    """Source verification."""

    enabled: bool = Field(
        default=True, description="Verify the quotes of every source."
    )


class ProjectConfig(StrictModel):
    """The spadework.toml file of a project."""

    schema_version: SchemaVersion
    project: ProjectSection
    models: ModelsSection = ModelsSection()
    limits: LimitsSection = LimitsSection()
    executor: ExecutorSection = ExecutorSection()
    verify: VerifySection = VerifySection()


def load_config(path: Path, display: str = CONFIG_FILE) -> ProjectConfig:
    document = load_toml(path, display, CONFIG_HINT)
    try:
        return ProjectConfig.model_validate(document.data)
    except ValidationError as exc:
        raise_for(exc, ProjectConfig, display, document.key_lines, CONFIG_HINT)


def render_config(name: str, playbook: str) -> str:
    """The spadework.toml that ``spadework init`` writes.

    The values are the model's defaults, written out so they are easy to
    find and change. A test keeps the two in step.
    """
    return f"""\
schema_version = 1

[project]
# A slug used in logs and the run manifest.
name = {_toml_string(name)}
# A built-in playbook name, or a path such as "./playbooks/mine".
playbook = {_toml_string(playbook)}

# Tasks declare a tier; this maps tiers to models.
[models]
draft = "sonnet"
review = "opus"

[limits]
budget_usd = 6.00          # per run
max_time = "2h"            # per run, such as "90m" or "2h"
rounds = 2                 # review rounds
session_timeout = "25m"    # per session

[executor]
name = "claude-cli"
# bin = "/path/to/claude"  # default: SPADEWORK_CLAUDE_BIN, then PATH

[verify]
enabled = true
"""


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        c if c >= " " or c == "\t" else f"\\u{ord(c):04x}" for c in escaped
    )
    return f'"{escaped}"'
