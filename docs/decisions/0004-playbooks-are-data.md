# 0004 — Playbooks are data, not code

Status: Accepted · Date: 2026-09-27

## Context

Playbooks are how spadework becomes useful beyond one domain, and sharing
them is the main way the community can contribute. If a playbook could run
code, then running someone else's playbook would mean trusting its author
with your machine.

## Decision

- A playbook consists of TOML, Markdown, and Jinja2 templates rendered in a
  sandboxed environment. Nothing else.
- Playbooks can configure tasks, dependencies, the built-in validators,
  research budgets, and additions to prompts. They cannot declare tools,
  hooks, executors, or Python code.
- Playbooks can **append** to the engine's base prompts, but they cannot
  replace the base rules that carry the truth and citation contract.

## Alternatives considered

- **Python playbooks** (a class per task). More expressive, but it makes
  every shared playbook a supply-chain risk.
- **Plugin entry points from day one.** Deferred. When custom validators
  arrive, they will be separately installed Python packages with explicit
  opt-in in `spadework.toml`, never code inside a playbook folder.

## Consequences

- Some logic that would be easy to write in code needs a built-in validator
  or engine feature. That is a deliberate brake on complexity.
- A playbook can still be harmful through its prompts, for example by asking
  for poor-quality work. The sandbox limits the damage to the pack. It
  cannot reach the machine.
