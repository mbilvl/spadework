"""JSON Schemas for spadework's file formats, generated from the pydantic models.

The .json files in this package are generated. Regenerate them after
changing a model:

    uv run python -m spadework.schemas
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from spadework.brief import BriefFrontMatter
from spadework.config import ProjectConfig
from spadework.playbook import PlaybookManifest, TaskFrontMatter

DIRECTORY = Path(__file__).parent

# File name → (model, title, description).
SCHEMAS: dict[str, tuple[type[BaseModel], str, str]] = {
    "spadework-toml.schema.json": (
        ProjectConfig,
        "spadework.toml",
        "The configuration of a spadework project.",
    ),
    "brief-front-matter.schema.json": (
        BriefFrontMatter,
        "brief.md front matter",
        "The YAML front matter of a project's brief. Other keys are allowed "
        "and reach templates as brief.extra.<key>.",
    ),
    "playbook-toml.schema.json": (
        PlaybookManifest,
        "playbook.toml",
        "The manifest of a spadework playbook.",
    ),
    "task-front-matter.schema.json": (
        TaskFrontMatter,
        "Task front matter",
        "The YAML front matter of a playbook task file. The body is a Jinja2 template.",
    ),
}


def schema_for(model: type[BaseModel], title: str, description: str) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True)
    schema["title"] = title
    schema["description"] = description
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def generate() -> dict[str, str]:
    """Every schema file's name and its expected content."""
    return {
        name: json.dumps(schema_for(*spec), indent=2, ensure_ascii=False) + "\n"
        for name, spec in SCHEMAS.items()
    }


def write(directory: Path | None = None) -> list[Path]:
    written = []
    for name, text in generate().items():
        path = (directory or DIRECTORY) / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
