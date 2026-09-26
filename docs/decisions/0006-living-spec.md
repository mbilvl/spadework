# 0006 — A living spec plus ADRs as the documentation of record

Status: Accepted · Date: 2026-09-27

## Context

spadework is open source from day zero. Design documents written up front
usually drift away from the code and turn into internal clutter.
Contributors need to know what the tool promises (file formats, CLI,
security) and why it was built this way.

## Decision

- **`docs/spec/`** is the normative description of behaviour. It covers the
  current behaviour plus the target of the next release, and each file
  carries a status line. The initial design document *is* the first version
  of the spec. There is no separate design document to keep in sync.
- **`docs/decisions/`** holds ADRs for the reasons behind decisions. They
  are immutable once accepted.
- **`docs/roadmap.md`** holds the milestones. Once GitHub milestones exist,
  it stays high-level.
- A pull request that changes behaviour updates the spec in the same pull
  request. The PR template asks for this.
- Nothing internal is committed: no real client material, transcripts, or
  private notes. Every example is fictional.

## Alternatives considered

- **A single design document plus code comments.** It goes stale, and there
  is nowhere to record rejected alternatives.
- **Only a docs site (mkdocs).** It can come later, built from the same
  Markdown files.
- **RFCs for every change.** Too heavy for a small project. An ADR plus the
  spec diff covers it.

## Consequences

- Reviewers check spec diffs alongside code diffs.
- The spec uses RFC 2119 keywords only for public contracts, so it stays
  readable.
