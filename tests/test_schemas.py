import json
from pathlib import Path

import pytest

import spadework.schemas
from spadework.schemas import DIRECTORY, SCHEMAS, generate, write


def test_shipped_schemas_are_up_to_date() -> None:
    for name, expected in generate().items():
        shipped = (DIRECTORY / name).read_text(encoding="utf-8")
        assert shipped == expected, (
            f"{name} is out of date. Run: uv run python -m spadework.schemas"
        )


def test_every_schema_is_shipped() -> None:
    shipped = {path.name for path in DIRECTORY.glob("*.json")}

    assert shipped == set(SCHEMAS)


def test_schemas_describe_the_formats() -> None:
    config = json.loads((DIRECTORY / "spadework-toml.schema.json").read_text())
    brief = json.loads((DIRECTORY / "brief-front-matter.schema.json").read_text())
    task = json.loads((DIRECTORY / "task-front-matter.schema.json").read_text())

    assert config["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert config["title"] == "spadework.toml"
    assert config["additionalProperties"] is False
    assert config["required"] == ["schema_version", "project"]
    assert config["properties"]["schema_version"]["const"] == 1
    assert brief["additionalProperties"] is True
    assert brief["required"] == ["title", "reader"]
    assert "validate" in task["properties"]
    assert "validate_" not in task["properties"]


def test_regenerating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setattr(spadework.schemas, "DIRECTORY", tmp_path)

    runpy.run_module("spadework.schemas", run_name="__main__")

    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(SCHEMAS)
    assert write(tmp_path) == [tmp_path / name for name in SCHEMAS]
