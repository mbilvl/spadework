# 0002 — Claude Code CLI is the first executor, behind an interface

Status: Accepted · Date: 2026-09-27

## Context

Sessions need web search, web fetch, file tools, per-tool-call hooks,
structured output, and cost reporting. Claude Code's headless mode
(`claude -p`) provides all of these today, and the prototype proved it
works for this job. The target users are likely to have Claude Code
installed already.

## Decision

- The default executor, `claude-cli`, runs the user's installed `claude`
  binary as a subprocess.
- The engine depends only on the `Executor` interface
  ([05](../spec/05-executors.md)). It never depends on CLI flags or on the
  `stream-json` format.
- spadework never handles credentials. Claude Code authenticates however the
  user has configured it.
- A `fake` executor ships from day one, for tests and demos.

## Alternatives considered

- **Claude Agent SDK first.** It runs in-process, and the guard could be a
  plain function. It is planned as the second executor. Starting with the
  CLI keeps v0 small and reuses what has already been proven.
- **Direct Messages API.** spadework would have to build and secure its own
  web search and fetch tools. It is planned for later.
- **Supporting several providers from the start.** That multiplies the
  testing surface before the core has proven itself.

## Consequences

- spadework depends on CLI flags and on an output format it does not
  control. Mitigations: the adapter is isolated, the tested minimum version
  is checked by `doctor`, the version is recorded in every manifest, and
  recorded stream fixtures run in CI.
- Behaviour can shift between CLI releases. `doctor --canary` is how a user
  confirms that the sandbox still holds on their installed version.
- Terms of use are the user's own relationship with Anthropic.
  Documentation should point this out, not interpret it.
