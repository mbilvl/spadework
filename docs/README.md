# spadework documentation

This folder is the documentation of record for spadework. It is written in the
open, for users and contributors, and it moves with the code.

| Folder | Answers | Rules |
|---|---|---|
| [`spec/`](spec/) | **What** spadework does and **how** | Normative. Describes current behaviour plus the behaviour targeted by the next release. A pull request that changes behaviour updates the spec in the same pull request. |
| [`decisions/`](decisions/) | **Why** it is built this way | Architecture Decision Records (ADRs). Immutable once accepted. To change a decision, write a new ADR that supersedes the old one. |
| [`roadmap.md`](roadmap.md) | **What's next** | Milestones and their acceptance criteria. Once the project has GitHub milestones, this file stays high-level and links to them. |
| [`guides/`](guides/getting-started.md) | **How do I…** | User-facing tutorials: [getting started](guides/getting-started.md), [writing a playbook](guides/writing-a-playbook.md). |

## Spec conventions

- Every spec file starts with a status line:
  `Status: Draft | Accepted | Implemented (vX.Y)`.
- Sections that are planned but not yet in the target release are marked
  **[later]**.
- The key words MUST, MUST NOT, SHOULD and MAY follow
  [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119). They are used only for
  public contracts: file formats, CLI behaviour, and security invariants.
- Examples use a fictional project, *Acme Fabrication*, throughout.

## What never goes in this repository

- Real client briefs, inputs, or generated packs. Every example is fictional.
- Personal notes, chat or agent transcripts, internal planning. If it
  matters to the project, write it up as a spec change, an ADR, or an issue.
- Secrets of any kind.

## Spec index

| # | Document | Covers |
|---|---|---|
| 00 | [Overview](spec/00-overview.md) | Problem, goals, concepts, architecture, principles |
| 01 | [CLI](spec/01-cli.md) | Commands, flags, output, exit codes, config precedence |
| 02 | [Project layout](spec/02-project-layout.md) | `spadework.toml`, `brief.md`, `inputs/`, `out/`, state |
| 03 | [Playbooks](spec/03-playbooks.md) | Playbook and task format, templates, validators, built-ins |
| 04 | [Engine](spec/04-engine.md) | Planning, caching, sessions, review loop, budgets, manifest |
| 05 | [Executors](spec/05-executors.md) | Executor interface, the `claude-cli` adapter, the fake executor |
| 06 | [Records](spec/06-records.md) | Structured session results, sources, citations, verification |
| 07 | [Security](spec/07-security.md) | Threat model, invariants, guard, residual risks |
