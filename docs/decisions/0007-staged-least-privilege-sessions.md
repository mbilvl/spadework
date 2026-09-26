# 0007 — Staged least-privilege sessions and a research/write split

Status: Accepted · Date: 2026-09-27

## Context

The prototype confined sessions to the project folder with a
`PreToolUse` hook. Testing that hook against crafted payloads showed that a
session could:

- write the settings file that configured the guard for every later session
- write the deadline file
- write the shared ledger that was injected into every later prompt
- write any document, not just its own
- call any MCP tool, because the guard used a denylist
- fetch arbitrary URLs while holding the full confidential brief

Each of these is reachable by prompt injection from a fetched web page.

## Decision

1. **Staging.** Each session runs in its own fresh directory, which holds
   copies of only the files it needs. The engine copies back only the single
   expected output.
2. **Control plane outside.** Policies, settings, counters, state and records
   live outside every staging directory. They are regenerated per session
   and never reused.
3. **Allowlist per session kind**, enforced by the CLI flags and again by
   the guard.
4. **Research/write split.** Only research sessions have web tools. They
   see only the user-written `public_context` and write nothing. Writing
   sessions have no web tools.

The resulting invariants are listed in [07 — Security](../spec/07-security.md).

## Alternatives considered

- **Fix the prototype's guard in place**, making its control files read-only
  but keeping one project-wide sandbox root. That closes the specific holes
  but still lets every session see every document and input, and it leaves
  the lethal trifecta in place.
- **Strip confidential text out of the brief automatically.** Unreliable.
  An explicit, user-written `public_context` is simpler and honest.
- **OS-level sandboxing** (containers, `sandbox-exec`, bubblewrap). Strong,
  but platform-specific and heavy for v0. It could be added later as an
  optional layer.

## Consequences

- Web-backed tasks cost one extra session each, for research.
- Writers work from research notes, not from live pages, so research quality
  depends on the notes and on quote verification.
- Users must write a `public_context`. That is a small cost, and it forces a
  deliberate decision about disclosure.
