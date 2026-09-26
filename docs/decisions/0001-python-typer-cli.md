# 0001 — Python, with a Typer + Rich CLI

Status: Accepted · Date: 2026-09-27

## Context

The prototype was 440 lines of bash. It embedded jq programs, used
macOS-only `date -j`, `launchctl` and `plutil`, and escaped XML by hand for
a plist. It was at the limit of what can be maintained, and it could not run
on Linux. The new tool has to be approachable for open-source contributors,
run on macOS and Linux, validate structured data, and generate JSON Schemas
for structured model output.

## Decision

- **Python ≥ 3.11**: `tomllib` is in the standard library, and the typing is modern.
- **Typer** for the command structure and **Rich** for terminal output.
- **pydantic v2** for every file format and result schema. Its JSON Schema
  export feeds `--json-schema` directly.
- Packaged with **hatchling**, and developed with **uv**. Users install with
  `pipx install spadework` or run `uvx spadework`.
- Runtime dependencies are kept to a small, deliberate set: typer, rich,
  pydantic, pyyaml, jinja2, httpx. Adding one requires a sentence of
  justification in the pull request.

## Alternatives considered

- **TypeScript.** Claude Code and the Agent SDK are TypeScript-first. It
  lost because Python's data-validation story is stronger, and it is the
  more common language for the research and automation audience.
- **Go.** A single static binary is attractive. It lost because the LLM
  tooling ecosystem is thinner and contributors would find the code less
  approachable.
- **Click directly.** Typer is built on Click, and its type-hint signatures
  keep commands short. Click remains the escape hatch if needed.

## Consequences

- Users need Python 3.11 or later. pipx and uvx make that painless.
- Typer and Rich add some import time. Keep `--help` and `--version` fast by
  importing heavy modules lazily.
- The guard hook (`spadework.sandbox.guard`) must avoid these dependencies,
  because it runs once per tool call. It uses the standard library only.
