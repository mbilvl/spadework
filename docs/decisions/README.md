# Architecture Decision Records

Short records of significant decisions: the context, what was decided, and
what follows from it. The format is a trimmed-down
[MADR](https://adr.github.io/madr/).

- Once an ADR is **Accepted**, it is not edited, apart from typo fixes and a
  pointer to the ADR that supersedes it.
- To change a decision, write a new ADR and mark the old one
  `Superseded by NNNN`.
- Write an ADR when a decision is hard to reverse, when it constrains
  contributors, or when it weakens a security invariant
  ([07](../spec/07-security.md)).

| # | Decision | Status |
|---|---|---|
| [0001](0001-python-typer-cli.md) | Python, with a Typer + Rich CLI | Accepted |
| [0002](0002-claude-code-cli-first-executor.md) | Claude Code CLI is the first executor, behind an interface | Accepted |
| [0003](0003-fresh-session-per-unit.md) | One fresh session per unit of work; state on disk | Accepted |
| [0004](0004-playbooks-are-data.md) | Playbooks are data, not code | Accepted |
| [0005](0005-no-agent-framework.md) | No third-party agent framework | Accepted |
| [0006](0006-living-spec.md) | A living spec plus ADRs as the documentation of record | Accepted |
| [0007](0007-staged-least-privilege-sessions.md) | Staged least-privilege sessions and a research/write split | Accepted |

## Template

```markdown
# NNNN — Title

Status: Proposed | Accepted | Superseded by NNNN · Date: YYYY-MM-DD

## Context
What forces are at play. What problem needs deciding.

## Decision
What we will do, stated plainly.

## Alternatives considered
Each with the one-line reason it lost.

## Consequences
What becomes easier, what becomes harder, and what we now have to maintain.
```
