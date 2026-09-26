# 0003 — One fresh session per unit of work; state on disk

Status: Accepted · Date: 2026-09-27

## Context

A research pack involves dozens of model interactions. A single long
conversation grows its transcript with every step. That raises the cost of
each later step, carries earlier mistakes forward, and loses everything if
it crashes. The prototype used a fresh session per task and passed only a
short ledger between sessions. Runs were resumable, and context size stayed
flat.

## Decision

- Every unit of work (research, draft, repair, critic, fix, summary) is a
  new session with no conversation history.
- Sessions share information only through files the engine stages for them
  and through structured records ([06](../spec/06-records.md)).
- `key_facts` in each record replaces the prototype's free-text ledger as
  the short memory passed downstream.
- All engine state is persisted after every session, so any run can be
  interrupted and resumed.

## Alternatives considered

- **One long agent conversation.** It is simpler to write, but it is
  expensive, fragile, and hard to resume.
- **Subagents inside one session.** Less control over each subagent's tools
  and budget, and it conflicts with I1 in the security spec.

## Consequences

- Each session repeats some fixed context (rules and the brief). Putting
  that in the stable system prompt keeps it cache-friendly.
- A session only knows what the engine gives it. Deciding what to stage is
  an engine responsibility and a design surface.
- Crash safety and `--only` reruns come almost for free.
