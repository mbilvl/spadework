# Getting started

This guide takes you from nothing to a first research pack. It uses the
small `quickstart` playbook, which runs two tasks and costs little. The
examples use a fictional project, *Acme Fabrication*.

## 1. Check your setup

You need Python 3.11 or later and [Claude Code](https://code.claude.com/docs)
installed and signed in: `claude` must work in your terminal. spadework runs
your local `claude` and never handles credentials itself.

```bash
spadework doctor
```

Each line is a check. Fix anything marked ✗ before you go on. To confirm that
the sandbox works on your machine, run the live canary. It runs one tiny
session, costs a few cents, and passes only if the sandbox blocks a read
outside the session's folder and a write to a second file:

```bash
spadework doctor --canary
```

If you'd like to try the commands first without spending anything, set
`SPADEWORK_EXECUTOR=fake`. The fake executor writes placeholder documents
instead of calling a model.

## 2. Create a project

```bash
spadework init acme --playbook quickstart
```

This creates `acme/` with:

| Path | What it is |
|---|---|
| `spadework.toml` | Settings: the playbook, models, budget and time limits |
| `brief.md` | What you know about the project. You write this. |
| `inputs/` | Material that arrives later, such as call notes |
| `.gitignore` | Keeps spadework's working state (`.spadework/`) out of git |

Without `--playbook`, `init` uses `discovery-website`, the full nine-task
discovery pack for a website project. `spadework playbooks list` shows what
is available.

## 3. Write the brief

Open `acme/brief.md`. The part between the `---` lines is read by spadework;
the comments in it explain each field. The ones that matter most:

- **`reader`**: who the documents are for. Every document is written for
  this person.
- **`public_context`**: the only text from your brief that web-facing
  research sessions ever see. Write it as if you were posting it publicly:
  what the project is, who it is for, and where. Leave out names, budgets and
  anything confidential.
- **`priorities`** (optional): what matters most. The review checks the pack
  against them.

Below the second `---`, write everything else you know, in plain Markdown.
It stays on your machine and is never sent to a research session.

## 4. Preview the run

```bash
spadework check -C acme
```

`check` validates everything the run will use and prints the plan: each
task, whether it needs to run, and an estimate. Estimates are ceilings, not
predictions; a typical run costs a fraction of them. Errors point to a file
and a line and say how to fix them.

To see exactly what research sessions will be given:

```bash
spadework check -C acme --show-public
```

## 5. Run it

```bash
spadework run -C acme
```

spadework researches, drafts, reviews and summarises, one sandboxed session
at a time. On a terminal you see a live view; otherwise progress goes to
stderr as plain lines. The run stops at the budget or time limit in
`spadework.toml` (`$6.00` and `2h` by default), or earlier with
`--budget-usd` and `--max-time`.

You can stop it with Ctrl-C at any time. Nothing is lost: the next `run`
carries on from where it stopped and doesn't redo finished work.

## 6. Read the pack

The pack is in `acme/out/`:

| File | What it is |
|---|---|
| `00-summary.md` | The executive summary, key decisions, open questions, and a table of every document's state |
| `01-…md`, `02-…md` | One document per task, each ending with its sources |
| `sources.md` | Every source, grouped by document, with its verification result |
| `pack.json` | The whole pack as data |

Every external fact cites a source such as `[S1]`. spadework fetches each
source and checks that the quoted passage really is on the page; the result
shows as a badge (✓ verified, ✗ quote not found, ? could not check). Claims
that rest only on unverified sources are tagged `[unverified]`.

The pack is a starting point for your own judgement. Read it critically
before you act on it.

## 7. Add new information

When the client answers a question or you have a call, save the notes in
`inputs/`, for example `inputs/2026-09-30-client-call.md`. Then:

```bash
spadework check -C acme
```

`check` shows which tasks are now stale and why, for example
*inputs/2026-09-30-client-call.md added*. `spadework run -C acme` redoes
only those tasks and the ones that depend on them.

## 8. Edit the documents

You can edit any file in `out/`. spadework notices, marks that document
*pinned*, and never overwrites it silently: other tasks use your version as
it stands. To have spadework redraft it anyway, replacing your edits:

```bash
spadework run -C acme --force --only questions
```

## When something goes wrong

| Command | Shows |
|---|---|
| `spadework status -C acme` | Each task's state and reason, and the last run |
| `spadework logs -C acme` | What each session of the last run did, call by call |
| `spadework logs -C acme landscape` | Only one task's sessions |

The exit code says how a run ended: `0` complete, `4` finished with some
tasks failed or invalid, `5` stopped at the budget or time limit, `6`
stopped by a rate limit, `7` another run holds the project, `130`
interrupted. After `5`, `6` or `130`, run again to resume. With
`--wait-on-rate-limit`, spadework waits for a rate limit to reset instead of
stopping.

Next: [writing a playbook](writing-a-playbook.md).
