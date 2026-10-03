# 01 — CLI

Status: Implemented (v0.1)

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
| `spadework init [DIR] [--playbook NAME] [--force]` | Create a project from a playbook | no |
| `spadework check [--json] [--show-public]` | Validate the project and print the plan. This is the dry run. | no |
| `spadework run [options]` | Run the pipeline | **yes** |
| `spadework status [--json]` | Task states, the last run, spend, and whether a run is active | no |
| `spadework logs [TASK] [--run ID] [--follow] [--raw] [--list]` | Show session traces | no |
| `spadework verify [--json]` | Re-verify the sources in the pack | no (network only) |
| `spadework playbooks list` / `show NAME` | List or show the built-in and local playbooks; `show` also prints the playbook's folder, to copy a built-in one | no |
| `spadework doctor [--canary] [--json]` | Check the environment. `--canary` runs a tiny live sandbox test. | `--canary` only (cents) |
| `spadework clean [--runs \| --cache \| --all] [--yes]` | Remove engine state. Never touches `spadework.toml`, `brief.md`, `inputs/` or `out/`. | no |
| `spadework --version` | Print the version | no |

### `init`

Creates `DIR` (default: the current directory) from a playbook. `--playbook`
defaults to `discovery-website` and also accepts a path to a playbook folder,
which is written to `spadework.toml` relative to the project. `DIR` must be
new or empty; a folder holding only `.git` counts as empty. A folder that
isn't empty is a usage error (exit `2`) unless `--force` is given. `--force`
replaces `spadework.toml` and `brief.md`, keeping each old file as
`NAME.bak` (`NAME.1.bak`, … if that is taken) unless it is unchanged, and
never touches `inputs/`. The project contains:

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
  references   stale   dependency audience is stale            $1.10
  ...
estimated spend for research and drafting: up to $3.20 (cap $6.00; the review and summary come out of the same budget)
```

`est.` is the task's `budget_usd`, plus the research estimate when the task
has research ([04](04-engine.md#budgets-and-limits)). A task that will reuse
its stored research is marked *research reused*, and its `est.` leaves the
research out; `--json` gives `research_reused` per task. Estimates are for
planning, not caps, so the total can exceed the budget without a problem: a
run that reaches the budget stops and can be resumed. `check` warns only
when a single session could never start within the budget.

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
| `--force` | off | Ignore the cache for the selected tasks. Regenerates a pinned output only together with `--only` (see below). |
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
budget cap is the safety net, and `check` is the preview. If nothing needs
doing, it prints `up to date` and exits `0`, or `4` when a task is still
invalid.

- Without `--only`, `--force` leaves pinned tasks alone with a warning;
  `--force --only TASK` replaces a person's edits to that task's document.
- `--refresh-research` applies to the tasks that run; it does not make a
  fresh task run.
- An invalid project (exit `3`), a bad option or an `--only` dependency that
  has no output yet (`2`), and a held lock (`7`) are found before the run
  starts, and write no manifest.

With `--json`, `run` prints one JSON object per line on stdout, each with
`schema_version`, `event` and `ts`:

| `event` | Fields |
|---|---|
| `plan` | the tasks (with `will_run`), limits, estimate, warnings |
| `up_to_date` | `exit_code`, the invalid tasks |
| `run_started` | run id, versions, limits, tasks |
| `session_started` | `n`, `task`, `kind`, `model`, `round`, `attempt`, `max_budget_usd`, `timeout_s` |
| `tool_call` | `n`, `name`, `target` |
| `denied` | as `tool_call`, plus `reason` and `possible_injection` |
| `session_finished` | `n`, `status`, `cost_usd`, `duration_s`, `guard_denials`, `spent_usd`, `error`, `retry` |
| `task_finished` | `task`, `state` (`fresh`, `invalid`, `failed` or `skipped`), `reason`, `validators` |
| `notice` | `message`, `hint`, `level`, `task` |
| `run_finished` | outcome, exit code, totals, message and hint, `rate_limit_reset`, `resume_after`, the files rendered, the tasks, `possible_injection`, the manifest path |
| `error` | the failure document, when the run could not start |

**Detached runs** are not a feature. Use your shell:
`nohup spadework run > run.log 2>&1 &`, then `spadework status` or
`spadework logs --follow`.

**Interrupts.** The first Ctrl-C stops the current session (SIGTERM to its
process group, then SIGKILL after 5 s), writes the manifest, and exits `130`.
A second Ctrl-C skips the 5 s wait and kills the session at once; the run
still records it and writes the manifest.

### `verify`

Re-checks the quote of every source in the pack's research records (network
only; no model, nothing spent), updates the records, and renders
`sources.md`, `pack.json`, the code-written parts of `00-summary.md` and each
document's Sources section again. Documents a person edited are never
touched, and the output names them. If the last run did not finish, only the
records are updated, and the next `run` renders them. It takes the project
lock and is bounded by `limits.max_time`; `[verify] enabled` applies to `run`
only. It exits `0` whatever the checks find, `3` for an invalid project and
`7` when the lock is held. With `--json` it prints `{schema_version, ok,
counts, sources, written, kept, render_owed, warnings}`, where each source
is `{id, task, url, status, detail, checked_at, final_url}`.

### `clean`

`--runs` removes `.spadework/runs/` (manifests and session logs), `--cache`
the research records (`.spadework/records/*.research.json`), so the next
run researches those tasks again, and `--all` the whole of `.spadework/`.
Give exactly one. It never touches `spadework.toml`, `brief.md`, `inputs/`
or `out/`, and it refuses (exit `3`) when `.spadework` is a symbolic link or
resolves outside the project. It says what it will remove and its size, holds the project lock
while removing (exit `7` if a run holds it), asks on a TTY, and needs `--yes`
otherwise (exit `2`). After `--all`, the next run has no record of the files
in `out/` and leaves them alone, so `clean` warns and suggests deleting
`out/` as well.

### `doctor`

One line per check, never wrapped, marked ✓, ✗ or ! (a warning): Python,
spadework, the executor and where it was chosen, the executor's own checks
(see [05](05-executors.md)), and, inside a project or the folder given with
`-C`, whether `check` passes and whether any input files are ignored. It exits `1` when a required check fails.

`--canary` runs one `haiku` session, capped at $0.05 (it can pass the cap by
one call), staged under the OS temporary directory and removed afterwards.
It asks the model to read a file outside its staging directory, to write a
second file, and to write its output. It passes only if the guard denied both
forbidden calls, the second file does not exist, the outside file's content
appears nowhere in the session, and the output was written. A step the model
skipped fails as *not tested*. It needs the `claude-cli` executor and the
other required checks to pass.

### `logs`

Shows the last run that has sessions (of `TASK`, if given), or `--run ID` (or
a unique prefix of it), with every session or only those of `TASK` (`critic`
and `summary` name the review's sessions). A later run that ended before any
session is noted and passed over. `--list` lists the runs, newest first, with
outcome, spend, sessions and start time. With no runs it prints `no runs
yet` and exits `0`. Each tool call is one line, with denied calls highlighted, text
snippets, and each session's status, cost, duration and denials. Logs are
replayed through the executor's own parser. `--raw` prints the JSONL as
recorded. `--follow` returns when the run ends. An unknown run or task exits
`2`.

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

Global options may be given before or after the command name, including
after `playbooks`. Without `-C`, the project is the first folder holding a
`spadework.toml`, from the current folder up, the way git finds a
repository; `init` never looks.

## Output conventions

- Results go to **stdout**: tables, JSON, and paths. Progress, logs and
  warnings go to **stderr**.
- `--json` makes `check`, `status`, `verify` and `doctor` print a single JSON document.
  With `run`, it prints NDJSON events. JSON output is a public contract, and
  every object has a `schema_version`. On failure the document is
  `{"schema_version": 1, "ok": false, "error": {"message", "hint",
  "exit_code", "problems": [{"path", "line", "message"}]}}`, and the
  human-readable error still goes to stderr.
- An error is one line that says what happened, followed by one line on what
  to do about it. No tracebacks unless `-v` or `SPADEWORK_DEBUG=1`.
- Paths in messages are relative to the current folder (absolute outside
  it); in JSON they are relative to the project. When `-C` names a project
  other than the current folder, the commands that messages suggest include
  it. A command that takes no arguments, given a folder, suggests `-C`.
- Costs from the `fake` executor are labelled *simulated* wherever they are
  shown.
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
| 6 | Stopped because of a rate limit. Resumable. The reset time is printed when the executor reports one. |
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
| `SPADEWORK_DEBUG` | Show tracebacks, and keep staging directories and control files |
| `SPADEWORK_FAKE_SCRIPT` | A JSON or TOML script for the `fake` executor ([05](05-executors.md)) |
| `NO_COLOR` | Disable colour |

## Help text

Every command's `--help` ends with at least one realistic example. Help text
is covered by tests, so changes to it are reviewed on purpose.
