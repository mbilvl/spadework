# 05 — Executors

Status: Draft · Target: v0.1

An executor runs one session and reports what happened. The engine never
talks to a model directly. Keeping this behind an interface from day one is
what lets other backends be added later without touching the engine
([ADR 0002](../decisions/0002-claude-code-cli-first-executor.md)).

## Interface

```python
class SessionKind(StrEnum):
    RESEARCH = "research"; DRAFT = "draft"; REPAIR = "repair"
    CRITIC = "critic"; FIX = "fix"; SUMMARY = "summary"

@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    kind: SessionKind
    prompt: str                    # the user message
    system_append: str             # engine base prompt + playbook additions
    model: str
    tools: tuple[str, ...]         # exact allowlist for this kind (see 07)
    workdir: Path                  # the staging directory; the session's whole world
    policy_path: Path              # guard policy, OUTSIDE workdir
    output_schema: dict[str, Any]  # JSON Schema for the structured result
    max_budget_usd: float
    timeout_s: int
    log_path: Path                 # raw event log, OUTSIDE workdir
    output: Path | None = None     # the one writable path; None for research and critic
    expected_tools: tuple[str, ...] = ()   # tools the session must report, checked at start
    task: str | None = None

@dataclass
class SessionResult:
    status: Literal["ok", "error", "timeout", "budget", "rate_limited", "interrupted"]
    structured: dict[str, Any] | None     # validated by the engine against the pydantic model
    cost_usd: float
    duration_s: float
    usage: Usage
    tool_calls: dict[str, int]
    rate_limit_reset: datetime | None
    error: str | None
    error_kind: Literal["transient", "schema", "sandbox", "executor"] | None
    guard_denials: int
    cost_known: bool

class Executor(Protocol):
    name: str
    def version(self) -> str | None: ...
    def diagnose(self) -> list[Diagnostic]: ...                      # used by `doctor`
    def run(self, spec: SessionSpec, events: EventSink) -> SessionResult: ...
```

A `SessionSpec` that breaks I3 or I4 ([07](07-security.md)) is refused when
it is built: research and critic sessions have no output, every other kind
has an output inside `workdir`, and `policy_path` and `log_path` are outside
it.

`error_kind` says why an `error` happened. `transient` errors are retried
([04](04-engine.md#budgets-and-limits)), `schema` errors are retried once,
and `sandbox` and `executor` errors are not retried. `cost_known` is false
when a session was stopped after the model started but before the CLI
reported its cost; the engine then charges the session's cap. An interrupt
stops the session and returns `interrupted` instead of raising, and the
engine then ends the run.

Events are normalised before they reach the engine, so the live view never
depends on any backend's wire format:

| Event | Fields |
|---|---|
| `ToolCall` | `name`, `target` (path, URL or query, shortened) |
| `Text` | `snippet` (one line) |
| `Denied` | `name`, `target`, `reason`: a tool call the guard denied |
| `RateLimit` | `status`, `resets_at` |
| `Finished` | the `SessionResult` |

## `claude-cli` (default)

Runs the user's installed Claude Code in headless mode. spadework never
reads, stores or passes credentials. Claude Code authenticates however the
user has configured it, and the user's own terms of service apply.

**Binary.** Taken from `SPADEWORK_CLAUDE_BIN`, then `[executor].bin`, then
`PATH`, following the configuration precedence in
[01](01-cli.md#configuration-precedence). `doctor` checks that it exists, runs it, and compares its version with
the minimum version this release was tested against, **2.1.278**. The version
is also recorded in every manifest.

**Invocation.** Confirmed by the M2 spike against Claude Code 2.1.278:

```
claude -p
  --model <model>
  --output-format stream-json --verbose
  --include-hook-events                     # needed for the liveness checks below
  --json-schema '<output_schema>'           # adds the StructuredOutput tool
  --tools <comma-separated allowlist>       # the exact set; checked against init
  --restricted                              # file tools confined to the cwd
  --permission-mode dontAsk                 # only calls the guard allows can run
  --permission-prompts none
  --settings <control>/<id>.settings.json   # guard hooks; memory and git context off
  --setting-sources ""                      # load no user, project or local settings
  --strict-mcp-config                       # no --mcp-config given → no MCP servers
  --disable-slash-commands                  # no skills or slash commands
  --no-session-persistence
  --exclude-dynamic-system-prompt-sections
  --append-system-prompt <system_append>
  --max-budget-usd <x>
stdin  = prompt
cwd    = workdir
```

The settings file is generated per session in `.spadework/control/`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "<guard> --event session-start", "timeout": 600 }] }],
    "PreToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "<guard> --event pre-tool-use", "timeout": 600 }] }]
  },
  "autoMemoryEnabled": false,
  "includeGitInstructions": false
}
```

`<guard>` is `<python> -I -m spadework.sandbox.guard --policy <abs path>`,
using the interpreter spadework itself runs under.

**What the spike established.**

| Question | Answer |
|---|---|
| Restricted mode | `--restricted` runs unattended with `--permission-mode dontAsk`. It confines Read, Glob, Grep, Write and Edit to the working directory, including absolute patterns and symlinks that point outside, and it held even when a test guard allowed everything. It refuses `bypassPermissions`. It is the default for every session kind. If `claude --help` runs and doesn't list it (an older CLI), spadework runs without it and warns, and so does `doctor`. If that check itself fails, the session is refused rather than run without restricted mode. |
| Permission mode | With `acceptEdits` plus `--allowedTools`, a guard that crashes, times out or cannot start lets the call through. With `dontAsk` and no pre-approved tools, a call runs only when the guard answers *allow*, so the guard fails closed. The exception is Read, Glob and Grep inside the working directory, which the CLI allows by itself; restricted mode confines them. `--allowedTools` is never passed. |
| Loading no settings | `--setting-sources ""`, with the empty string as its own argument (`none` is rejected). Hooks from `--settings` still run. Without it, the user's and the project's hooks, MCP servers (started even when `--tools` excludes them), `CLAUDE.md` files, output style and enabled plugins all reach the session. |
| Remaining leaks | Auto-memory and the git status block (branch, git user name, changed files, recent commit subjects) are injected even under `--setting-sources ""` and `--restricted`. The settings file turns both off, and so do the environment variables below, which also cover a settings file the CLI silently ignored. `--disable-slash-commands` removes the bundled skills. |
| Structured result | `--json-schema` adds a `StructuredOutput` tool, which the guard must allow. The result arrives in the final `result` event as `structured_output`. A session is `ok` only if `subtype` is `success`, `is_error` is false, and `structured_output` is present. Output that fails the schema is retried by the CLI up to 5 times, then ends with `error_max_structured_output_retries`. A model that never calls the tool ends with `success` and no `structured_output`, which spadework treats as a schema failure ([06](06-records.md)). |
| Tool names | `Read`, `Glob`, `Grep`, `Write`, `Edit`, `WebSearch`, `WebFetch`, plus `StructuredOutput`. `--tools` sets the exact set of built-in tools, but silently drops names it doesn't know, so the adapter compares the tools listed in the `init` event with the expected set and fails the session if they differ. |
| Hook timeout | The default is 600 s. The guard takes a few milliseconds, including its file-locked counters, so the timeout needs no change. A hook that times out does not block the call, which is why failing closed relies on `dontAsk`. |
| Never used | `--bare` and `--safe-mode` skip every hook, including the guard. `bypassPermissions`, `--dangerously-skip-permissions`, `--allowedTools`, `--add-dir`, `--mcp-config`, `--plugin-*`, `--agents` and `--fallback-model` are never passed. |

**Liveness checks.** `-p` silently ignores a settings file it cannot parse,
so the adapter checks that the guard is really running:

- Nothing but hook events may arrive before `init`. A model message, tool
  result or `result` event before it is a sandbox violation.

- The guard also runs as a `SessionStart` hook, which fires before the
  `init` event and before any model call. If no successful `SessionStart`
  hook response has arrived by `init`, the session is killed and fails with
  *the sandbox guard did not start*.
- Every tool call that runs uses up one *allow* from the guard for that
  tool. A `PreToolUse` hook response must have exit code `0` (the guard's
  decision) or `2` (the guard blocked the call after an internal error). Any
  other exit code, a successful tool result with no unused allow, or a
  result for a call the stream never announced kills the session and fails
  it.
- The guard's `session-start` check also writes a marker file in the control
  directory, which the executor checks for at `init`. A `SessionStart` hook
  from managed settings therefore cannot stand in for the guard.

**Parsing.** The adapter reads `stream-json` line by line and writes every
raw line to `log_path`. Unknown event types are ignored. It is tolerant of
additions and strict about the fields it needs. From the `result` event it
takes `total_cost_usd`, `usage` (including `server_tool_use` for web
searches and fetches), `duration_ms`, and `permission_denials`, whose count
is the session's guard denials. `error_max_budget_usd` maps to `budget`;
`--max-budget-usd` is checked after each model call, so a session can
overshoot its cap by one call. A `rate_limit_event` whose
`rate_limit_info.status` is `rejected` maps to `rate_limited`, with the reset
time from `resetsAt`, and so does an API error with status 429. A stream
that ends without a `result` event is an `error`. Matching on error text is
only a fallback for detecting rate limits. A session that ends with `success`
and a structured result is `ok` even if a `rejected` rate-limit event arrived
during it; `rate_limit_reset` is still set, so the engine stops before the
next session. API errors with a 5xx status are `transient`.

**Process control.** The CLI starts in its own process group
(`start_new_session=True`). A timeout or interrupt sends SIGTERM to the
group, waits 5 s, then sends SIGKILL. A sandbox violation sends SIGKILL at
once. Once the `result` event has arrived, the CLI gets a short grace period
to exit, and a result that arrived before the deadline counts even if the
process has to be stopped. After every session the whole group is killed,
so nothing the CLI started outlives it. A CLI that exits with an error
before `init` (a bad flag, for example) is an `executor` error and is not
retried.

**Environment.** The child inherits the user's environment, because the CLI
needs it for authentication and proxies. Sessions have no shell, so they
cannot read it. spadework removes `CLAUDE_CODE_SIMPLE` and
`CLAUDE_CODE_SAFE_MODE`, which would disable every hook, and sets
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and
`CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`. A session started from inside
another Claude Code session can fail to authenticate; `doctor` runs
`claude auth status` to catch this early.

## `fake` (tests and demos)

A deterministic executor for tests and for trying the CLI without spending
anything (`SPADEWORK_EXECUTOR=fake`).

- It plays back scripted behaviour per session kind: it writes a plausible
  output file into the staging dir and returns a schema-valid result.
- It can replay a recorded `stream-json` fixture through the real
  `claude-cli` parser, which tests the parser against real CLI output
  without calling a model.
- It can inject failures (timeout, rate limit, invalid schema, missing
  output, a guard denial) to test the engine's error paths.

CI runs the whole pipeline end-to-end with the fake executor.

## Future executors **[later]**

| Executor | Why |
|---|---|
| `agent-sdk` | The Claude Agent SDK runs in-process. The guard becomes a function in place of a subprocess and settings file. It also gives native streaming, cost data and structured output. |
| `api` | Direct Messages API with spadework's own web tools. Needs no Claude Code install. |

Every executor must pass the same conformance tests: the guard canary,
schema handling, timeouts, and rate limits.
