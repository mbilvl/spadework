"""Shared building blocks for the pydantic models of spadework's file formats."""

from __future__ import annotations

import difflib
import re
import typing
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from spadework.durations import DURATION_PATTERN, parse_duration
from spadework.errors import InvalidError, Problem
from spadework.textfiles import KeyLines

SCHEMA_VERSION = 1


class StrictModel(BaseModel):
    """Unknown keys are errors, and values are not coerced between types."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def matching(pattern: str, description: str) -> AfterValidator:
    """Check a string against ``pattern``, with an error a person can act on."""
    compiled = re.compile(pattern)

    def check(value: str) -> str:
        if not compiled.fullmatch(value):
            raise ValueError(f"{value!r} is not valid: {description}")
        return value

    return AfterValidator(check)


def schema_pattern(pattern: str) -> Any:
    """Put ``pattern`` in the JSON Schema without pydantic's own error."""
    return Field(json_schema_extra={"pattern": f"^(?:{pattern})$"})


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _schema_version(value: int) -> int:
    if value != SCHEMA_VERSION:
        raise ValueError(
            f"schema_version {value} is not supported; "
            f"this version of spadework reads schema_version {SCHEMA_VERSION}"
        )
    return value


def _duration(value: str) -> str:
    parse_duration(value)
    return value


SLUG = r"[a-z0-9][a-z0-9-]*"
TASK_ID = r"[a-z][a-z0-9-]*"

NonEmptyStr = Annotated[
    str, AfterValidator(_not_blank), Field(json_schema_extra={"pattern": r"\S"})
]
SchemaVersion = Annotated[
    int,
    AfterValidator(_schema_version),
    Field(
        description="The version of this file format. Always 1.",
        json_schema_extra={"const": SCHEMA_VERSION},
    ),
]
Duration = Annotated[
    str,
    AfterValidator(_duration),
    Field(json_schema_extra={"pattern": DURATION_PATTERN}),
]
Slug = Annotated[
    str,
    matching(SLUG, "use lowercase letters, digits and '-', such as 'acme-fabrication'"),
    schema_pattern(SLUG),
]
TaskId = Annotated[
    str,
    matching(TASK_ID, "start with a lowercase letter and use only a-z, 0-9 and '-'"),
    schema_pattern(TASK_ID),
]
Tier = typing.Literal["draft", "review"]
Usd = Annotated[float, Field(gt=0, allow_inf_nan=False)]


def validation_problems(
    exc: ValidationError,
    model: type[BaseModel],
    display: str,
    key_lines: KeyLines,
) -> list[Problem]:
    """Turn a pydantic error into problems that name the key and its line."""
    problems = []
    for error in exc.errors(include_url=False):
        loc = tuple(error["loc"])
        key = _dotted(loc)
        kind = error["type"]
        if kind == "missing":
            message = f"'{key}' is required"
        elif kind == "extra_forbidden":
            message = f"unknown key '{key}'"
            allowed = _allowed_keys(model, tuple(error["loc"][:-1]))
            close = difflib.get_close_matches(str(loc[-1]), allowed, n=1)
            if close:
                message += f" (did you mean '{close[0]}'?)"
        else:
            message = f"{key}: {_describe(error)}" if key else _describe(error)
        problems.append(Problem(message, display, _line_for(loc, key_lines)))
    return problems


def raise_for(
    exc: ValidationError,
    model: type[BaseModel],
    display: str,
    key_lines: KeyLines,
    hint: str,
) -> typing.NoReturn:
    for error in exc.errors(include_url=False):
        if _read_as_boolean(error):
            # YAML 1.1 reads unquoted yes/no/on/off as booleans: `language: no`
            # is false, not Norwegian.
            key = next((p for p in reversed(error["loc"]) if isinstance(p, str)), "key")
            word = "yes" if error["input"] else "no"
            hint = (
                "YAML reads unquoted yes, no, on, off, true and false as true or "
                f'false. Put the value in quotes, for example {key}: "{word}".'
            )
            break
    raise InvalidError(
        f"{display} is not valid",
        hint,
        problems=validation_problems(exc, model, display, key_lines),
    ) from exc


def _read_as_boolean(error: Any) -> bool:
    return error["type"] == "string_type" and isinstance(error.get("input"), bool)


def _dotted(loc: tuple[str | int, ...]) -> str:
    text = ""
    for part in loc:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text


def _describe(error: Any) -> str:
    kind = error["type"]
    message = str(error["msg"])
    if kind == "value_error":
        return str(error["ctx"]["error"])
    if kind in {"dict_type", "model_type"}:
        return "should be a table of keys and values"
    if _read_as_boolean(error):
        return f"should be text, but YAML read it as {str(error['input']).lower()}"
    for subject in ("Input ", "String ", "List ", "Value "):
        if message.startswith(subject + "should"):
            return message[len(subject) :]
    return message[:1].lower() + message[1:]


def _line_for(loc: tuple[str | int, ...], key_lines: KeyLines) -> int | None:
    # The deepest key we know the line of: a missing key reports its parent.
    for end in range(len(loc), 0, -1):
        if (line := key_lines.get(loc[:end])) is not None:
            return line
    return None


def _allowed_keys(model: type[BaseModel], loc: tuple[str | int, ...]) -> list[str]:
    """The keys allowed at ``loc``, for suggesting a fix for a typo."""
    current = model
    for part in loc:
        fields = current.model_fields.items()
        field = next((f for name, f in fields if (f.alias or name) == part), None)
        nested = None if field is None else _model_in(field.annotation)
        if nested is None:
            return []
        current = nested
    return [field.alias or name for name, field in current.model_fields.items()]


def _model_in(annotation: Any) -> type[BaseModel] | None:
    """The model in ``Model`` or ``Model | None``."""
    for candidate in (annotation, *typing.get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None
