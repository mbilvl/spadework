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
    log_path: Path                 # raw event log

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

class Executor(Protocol):
    name: str
    def diagnose(self) -> list[Diagnostic]: ...                      # used by `doctor`
    def run(self, spec: SessionSpec, events: EventSink) -> SessionResult: ...
```

Events are normalised before they reach the engine, so the live view never
depends on any backend's wire format:

| Event | Fields |
|---|---|
| `ToolCall` | `name`, `target` (path, URL or query, shortened) |
| `Text` | `snippet` (one line) |
| `RateLimit` | `status`, `resets_at` |
| `Finished` | the `SessionResult` |

## `claude-cli` (default)

Runs the user's installed Claude Code in headless mode. spadework never
reads, stores or passes credentials. Claude Code authenticates however the
user has configured it, and the user's own terms of service apply.

**Binary.** Taken from `[executor].bin`, then `SPADEWORK_CLAUDE_BIN`, then
`PATH`. `doctor` checks that it exists, runs it, and compares its version with
the minimum version this release was tested against. The version is also
recorded in every manifest.

**Invocation (baseline, to be confirmed by the M2 spike):**

```
claude -p
  --model <model>
  --output-format stream-json --verbose
  --json-schema '<output_schema>'
  --tools <comma-separated allowlist>
  --settings <control>/settings.json        # PreToolUse hook → guard; permissions.deny
  --strict-mcp-config                       # no --mcp-config given → no MCP servers at all
  --setting-sources <none>                  # do not inherit user/project/local settings
  --append-system-prompt <system_append>
  --max-budget-usd <x>
  --permission-mode <mode>
  --disable-slash-commands
  --no-session-persistence
  --exclude-dynamic-system-prompt-sections
stdin  = prompt
cwd    = workdir
```

**Open items for the M2 spike.** Each must be answered and written into this
section before M2 closes:

1. **`--restricted`.** The installed CLI offers a restricted mode. It
   confines the file tools to the working directories, ignores user, project
   and local settings, and removes code-running tools. It is a strong second
   layer on top of the guard. It refuses `bypassPermissions`, so the spike
   must find an unattended permission mode that works with it (candidate:
   `acceptEdits` plus `--allowedTools` covering exactly the allowlist). If
   that works, restricted mode is the default. If not, record why and fall
   back to `bypassPermissions` with the guard.
2. How `--setting-sources` expresses "load nothing".
3. Where the structured result appears in `stream-json` output when
   `--json-schema` is set, and what happens when the model's output fails
   the schema.
4. The exact tool names to allowlist in the current CLI version (Glob and
   Grep may have been renamed or merged).
5. Whether the hook `timeout` needs raising for the guard's file-locked counters.

**Parsing.** The adapter reads `stream-json` line by line and writes every
raw line to `log_path`. Unknown event types are ignored. It is tolerant of
additions and strict about the fields it needs. Cost comes from
`total_cost_usd`, and rate-limit state from `rate_limit_event`. A stream that
ends without a `result` event is an `error`. Matching on error text is only a
fallback for detecting rate limits.

**Process control.** The CLI starts in its own process group
(`start_new_session=True`). A timeout or interrupt sends SIGTERM to the
group, waits 5 s, then sends SIGKILL.

**Environment.** The child inherits the user's environment, because the CLI
needs it for authentication and proxies. Sessions have no shell, so they
cannot read it.

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
