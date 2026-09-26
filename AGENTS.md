# AGENTS.md

Guidance for AI coding assistants working on spadework, and for the people
who use them. [CONTRIBUTING.md](CONTRIBUTING.md) is the full guide. This is
the short version an assistant can load before it starts.

## The project

spadework is a Python CLI that runs research playbooks as sandboxed Claude
Code sessions and produces a sourced research pack. What it does is in
[`docs/spec/`](docs/spec/00-overview.md), which is normative. Why it is
built this way is in [`docs/decisions/`](docs/decisions/README.md). What
comes next is in [`docs/roadmap.md`](docs/roadmap.md).

## Commands

```bash
uv sync                              # set up .venv with the dev tools
uv run pytest --cov                  # tests, offline, with coverage
uv run ruff check .                  # lint
uv run ruff format .                 # format
uv run mypy src                      # strict type check
uv run pre-commit run --all-files    # everything the commit hook runs
uv run spadework --help              # the CLI
```

Run the tests, lint, format check and type check before calling a change done.

## Where things are

| Path | What |
|---|---|
| `src/spadework/` | The package. `cli.py` is the Typer app and stays thin. |
| `src/spadework/builtin/playbooks/` | Built-in playbooks. Data only. |
| `tests/` | pytest. Offline and deterministic. |
| `examples/acme-fabrication/` | A fictional example project |
| `docs/` | Spec, ADRs, roadmap |

## Rules

- **Read the spec before changing behaviour**, and update `docs/spec/` in
  the same pull request. If the code and the spec disagree, raise it rather
  than quietly picking one.
- **Enforce in code, not in prompts.** Sandboxing, limits, schemas and
  budgets are checked by the engine.
- **Security invariants** ([07](docs/spec/07-security.md)) are never
  weakened without an ADR. The guard (`spadework.sandbox.guard`) uses the
  standard library only, and `import spadework` stays free of imports.
- **Playbooks are data**: TOML, Markdown and sandboxed Jinja2. No code.
- **Dependencies**: the runtime set is typer, rich, pydantic, pyyaml,
  jinja2 and httpx. Adding one needs a sentence of justification.
- **Tests never call a model.** Use the fake executor. Don't run `claude`
  from tests, and don't spend money.
- **CLI conventions** ([01](docs/spec/01-cli.md)): results on stdout,
  progress on stderr; an error is one line on what happened and one on what
  to do; exit codes as specified. Keep `--help` and `--version` fast by
  importing heavy modules lazily.
- Match the surrounding code. Docs are in British English.

## Never commit

- Real client briefs, inputs, packs, or `.spadework/` logs. Every example
  is fictional and uses *Acme Fabrication*.
- Chat or agent transcripts, assistant memory files, personal notes or plans.
- Secrets, tokens, or local paths that contain a username. This includes
  test fixtures such as recorded CLI output: sanitise them first.
- Personal assistant configuration. This file is the shared one.

## Commits

Sign off every commit with `git commit -s`. The sign-off is the human
contributor certifying the [DCO](https://developercertificate.org/), so
review what the assistant wrote before you sign it.
