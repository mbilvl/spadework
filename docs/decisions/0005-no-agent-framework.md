# 0005 — No third-party agent framework

Status: Accepted · Date: 2026-09-27

## Context

Frameworks such as LangGraph and CrewAI offer graphs, memory, and
multi-agent patterns. spadework's orchestration needs are small: a
dependency sort, a cache, a bounded review loop, and budgets. Its value lies
in its contracts (playbooks, schemas, validators, and the sandbox).

## Decision

The engine is plain Python and has no agent-framework dependency.

## Alternatives considered

- **LangGraph.** Well suited to complex stateful graphs. That is more than
  v0 needs, and it would pull in a large dependency tree plus concepts
  contributors would have to learn.
- **CrewAI and similar "agent crew" libraries.** Their role-play
  abstractions hide the very things spadework must control precisely:
  exactly which tools each session has, and exactly what it can see.

## Consequences

- There is more code of our own for scheduling and state, but it is small,
  explicit, and tested with the fake executor.
- If spadework grows parallel execution or a branching workflow, this
  decision should be revisited.
