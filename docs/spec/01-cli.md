# 01 — CLI

Status: Draft · Target: v0.1

The CLI is the product. It follows the [Command Line Interface
Guidelines](https://clig.dev/): output is for humans first, JSON is available
when you ask for it, errors say what went wrong, and exit codes are
predictable. It is built with Typer and Rich
([ADR 0001](../decisions/0001-python-typer-cli.md)).

The command is `spadework`, and `python -m spadework` works too. There is no
short alias, to avoid colliding with other tools. Users who want one can set a
shell alias.

## Commands

| Command | Purpose | Spends money |
|---|---|---|
| `spadework init [DIR] [--playbook NAME]` | Create a project from a playbook | no |
| `spadework check [--json] [--show-public]` | Validate the project and print the plan. This is the dry run. | no |
| `spadework run [options]` | Run the pipeline | **yes** |
| `spadework status [--json]` | Task states, the last run, spend, and whether a run is active | no |
| `spadework logs [TASK] [--run ID] [--follow] [--raw]` | Show session traces | no |
| `spadework verify [--json]` | Re-verify the sources in the pack | no (network only) |
| `spadework playbooks list` / `show NAME` | List or show the built-in and local playbooks | no |
| `spadework doctor [--canary]` | Check the environment. `--canary` runs a tiny live sandbox test. | `--canary` only (cents) |
| `spadework clean [--runs \| --cache \| --all] [--yes]` | Remove engine state. Never touches `brief.md` or `inputs/`. | no |
| `spadework --version` | Print the version | no |

### `init`

Creates `DIR` (default: the current directory) from a playbook. `--playbook`
defaults to `discovery-website` and also accepts a path to a playbook folder,
which is written to `spadework.toml` relative to the project. `DIR` must be
new or empty; a folder holding only `.git` counts as empty. A folder that
isn't empty is a usage error (exit `2`) unless `--force` is given. `--force`
replaces `spadework.toml` and `brief.md` with a warning and never touches
`inputs/`. The project contains:

- `spadework.toml`
- `brief.md`, copied from the playbook's brief template with its required
  fields commented
- an empty `inputs/`
- a `.gitignore` that ignores `.spadework/`

It finishes by printing the next step: *edit brief.md, then run
`spadework check`*.

### `check`

Loads and validates everything `run` would use, then prints the plan:

```
playbook  discovery-website 0.1.0
budget    $6.00 · 2h · 2 review rounds

  task         state   reason                                  est.
  scope        fresh                                           —
  audience     stale   inputs/2026-09-30-client-call.md added  $0.40
  references   stale   depends on: audience                    $1.10
  ...
estimated spend for pending and stale work: up to $3.20 (cap $6.00)
```

`est.` is the task's `budget_usd`, plus the research estimate when the task
has research ([04](04-engine.md#budgets-and-limits)). It is a ceiling, not a
prediction, so the total can exceed the cap without a problem: a run that
reaches the cap stops and can be resumed. `check` warns only when a single
task could never start within the budget.

- `--show-public` prints everything from the project that web-facing
  research sessions will see: `public_context` and each research task's
  rendered questions. See [07 — Security](07-security.md). With `--json`, it
  adds a `public` key to the document.
- `check` warns when the playbook has research tasks and `public_context` is
  empty.
- Exits `3` on any validation error. Errors point to a file, and to a line
  where possible, and say how to fix it.

### `run`

| Option | Default | Meaning |
|---|---|---|
| `--only TASK` (repeatable) | all | Run only these tasks. Dependencies are not rerun, and their existing outputs are used. |
| `--force` | off | Ignore the cache for the selected tasks. Also regenerates pinned outputs. |
| `--refresh-research` | off | Redo research sessions even when their fingerprint matches |
| `--no-review` | off | Skip the review rounds |
| `--rounds N` | config (2) | Maximum number of review rounds |
| `--budget-usd X` | config | Spending cap for this run |
| `--max-time D` | config | Wall-clock cap, for example `90m` or `2h` |
| `--wait-on-rate-limit` | off | Sleep until the usage window resets instead of stopping, within `--max-time` |
| `--no-verify` | off | Skip source verification |
| `--draft-model M` / `--review-model M` | config | Override the model for each tier |
| `--json` | off | Emit NDJSON events on stdout in place of the live view |

`run` prints the plan, then starts. It does not ask for confirmation. The
budget cap is the safety net, and `check` is the preview. If nothing is stale,
it prints `up to date` and exits `0`.

**Detached runs** are not a feature. Use your shell:
`nohup spadework run > run.log 2>&1 &`, then `spadework status` or
`spadework logs --follow`.

**Interrupts.** The first Ctrl-C stops the current session (SIGTERM to its
process group, then SIGKILL after 5 s), writes the manifest, and exits `130`.
A second Ctrl-C exits immediately.

### Live view

On a TTY, a Rich live display shows:

```
spadework · acme-fabrication · discovery-website 0.1.0 · round 1/2

  ✓ scope        draft     sonnet  $0.21  1m12s
  ✓ audience     draft     sonnet  $0.18  0m58s
  ▸ references   research  sonnet  $0.34  2m40s  → WebFetch peer-site.example
  · ia           pending
  ⚠ references   2 tool calls blocked by the sandbox (see: spadework logs references)

spent $0.73 of $6.00 · 4m50s of 2h
```

When the output is not a TTY, the same information goes to stderr as
timestamped plain lines, one per event.

## Global options

| Option | Meaning |
|---|---|
| `-C, --project PATH` | Run as if started in `PATH`, like `git -C` |
| `-q, --quiet` / `-v, --verbose` | Less or more output. `-v` shows tracebacks. |
| `--no-color` | Disable colour. The `NO_COLOR` environment variable is also honoured. |

Global options may be given before or after the command name.

## Output conventions

- Results go to **stdout**: tables, JSON, and paths. Progress, logs and
  warnings go to **stderr**.
- `--json` makes `check`, `status` and `verify` print a single JSON document.
  With `run`, it prints NDJSON events. JSON output is a public contract, and
  every object has a `schema_version`. On failure the document is
  `{"schema_version": 1, "ok": false, "error": {"message", "hint",
  "exit_code", "problems": [{"path", "line", "message"}]}}`, and the
  human-readable error still goes to stderr.
- An error is one line that says what happened, followed by one line on what
  to do about it. No tracebacks unless `-v` or `SPADEWORK_DEBUG=1`.
- Destructive commands (`clean`) ask for confirmation on a TTY. Without a TTY
  they require `--yes`.
- When a command has an obvious next step, it suggests it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. For `run`, the pack is complete and every validator passes. |
| 1 | Unexpected error, or the executor failed |
| 2 | Usage error: bad flags or arguments |
| 3 | Invalid project, brief, or playbook |
| 4 | Partial: the run finished, but some tasks failed or are invalid |
| 5 | Stopped because the budget or time limit was reached. Resumable. |
| 6 | Stopped because of a rate limit. Resumable. The reset time is printed. |
| 7 | Another run holds the project lock |
| 130 | Interrupted |

## Configuration precedence

Highest wins: command-line flags, then environment variables (`SPADEWORK_*`),
then `spadework.toml`, then built-in defaults. A user-level config file
(`~/.config/spadework/config.toml`) is **[later]**.

| Environment variable | Meaning |
|---|---|
| `SPADEWORK_CLAUDE_BIN` | Path to the `claude` binary |
| `SPADEWORK_EXECUTOR` | Executor name. `fake` is for tests and demos. |
| `SPADEWORK_DEBUG` | Show tracebacks |
| `NO_COLOR` | Disable colour |

## Help text

Every command's `--help` ends with at least one realistic example. Help text
is covered by tests, so changes to it are reviewed on purpose.
