# Writing a playbook

A playbook is a reusable research recipe: the tasks to run, how to write
each document, and what the review should check. Playbooks are data, made of
TOML, Markdown and Jinja2 templates. They cannot run code, so you can use a
playbook written by someone else safely. The full format is in
[03 — Playbooks](../spec/03-playbooks.md); this guide walks through it.

## Start from a copy

The quickest way in is to copy a built-in playbook and change it.
`spadework playbooks show quickstart` shows its tasks. The built-ins live in
the package under `spadework/builtin/playbooks/`; this repository has them
in `src/spadework/builtin/playbooks/`.

A playbook is a folder:

```
market-scan/
├── playbook.toml
├── brief.template.md       # copied into brief.md by `spadework init`
├── tasks/
│   ├── competitors.md
│   └── pricing.md
└── prompts/                # optional additions to the engine's prompts
    └── rules.md
```

Point a project at it with a path, relative to the project:

```bash
spadework init acme --playbook ./playbooks/market-scan
```

or set `playbook = "./playbooks/market-scan"` in `spadework.toml`.

## `playbook.toml`

```toml
schema_version = 1
name = "market-scan"
version = "0.1.0"
description = "Who else serves this market, and how they price."
requires_brief = ["priorities"]   # brief fields this playbook needs

[defaults]                        # task settings that tasks can override
tier = "draft"
budget_usd = 0.50
max_lines = 150

[review]
max_findings = 8
```

Raise `version` whenever you change the playbook. It is part of every
task's fingerprint, so the next run redoes the work.

## Tasks

Each file in `tasks/` is one task. Its name, without `.md`, is the task id.
The front matter says what the task is; the body tells the drafting model
what to write.

```markdown
---
title: Competitor pricing
output: 02-pricing.md
depends_on: [competitors]
research:
  max_searches: 4
  max_fetches: 6
  questions: |
    For {{ (public_context | trim) }}:
    - the published prices of the closest competitors, with what each tier includes
validate:
  max_lines: 120
  min_sources: 3
  required_headings: [Prices, What stands out, Gaps]
---
For the reader, compare how the competitors in 01-competitors.md price
their offer.

Write these sections as `##` headings: **Prices** (a table), **What stands
out**, and **Gaps** (what the research could not find).

{% if brief.currencies %}Give prices in {{ brief.currencies | join(" and ") }}.{% endif %}
```

| Field | Notes |
|---|---|
| `title`, `output` | Required. `output` is the file name in `out/`. |
| `depends_on` | Tasks whose documents this one builds on. They run first, and their documents and key facts are given to this task. |
| `tier` | `draft` or `review`; `spadework.toml` maps each to a model. Use `review` where a stronger model clearly pays off. |
| `inputs` | Globs over `inputs/` (default: all). Only matching inputs are given to the task, and only they make it stale. |
| `budget_usd` | The task's cost estimate, which also sets its session's spending cap. |
| `research` | Adds a web research session before the draft. |
| `validate` | Checks run in code after every draft (see below). |

## Research questions see only `public_context`

The `questions` template of a research task can use only `public_context`,
`task` and `brief.language`. This is enforced: research sessions have web
access, so they never see the rest of the brief, the inputs, or other
documents. Write questions that make sense to someone who knows only what
`public_context` says. `spadework check --show-public` shows exactly what
research sessions will receive.

## Task bodies

The body is a Jinja2 template with access to `brief` (its fields,
`brief.extra` for any other front-matter key, and `brief.body`), `task`,
`project`, `files`, `deps` and `research`. A missing variable is an error in
`check`, not an empty string, so guard optional fields:

```
{% if brief.extra.launch_target is defined and brief.extra.launch_target %}…{% endif %}
```

You don't need to repeat what the engine already gives every draft session:
the staged files, each dependency's key facts, the research notes and their
sources, and the truth and citation rules. Spend the body on what is
specific to this task:

- who the document is for and what they will do with it;
- the sections, matching `required_headings` exactly;
- how to use the dependencies and the research;
- what to do when information is missing: name it as a gap or an open
  question, never fill it in.

## Validators

Validators run in code after every draft. When one fails, the engine runs
one repair session with the failure messages.

| Validator | Checks |
|---|---|
| `max_lines`, `min_lines` | Line count |
| `required_headings` | Each heading appears, at any level |
| `starts_with_h1` | The document starts with `# …` (on by default) |
| `forbidden_phrases` | Phrases that must not appear, on top of the engine's |
| `max_open_questions` | Open questions in the record (default 6) |
| `citations_resolve` | Every `[S#]` exists in the task's sources; a task without research cites none (on by default) |
| `min_sources` | The document cites at least this many sources (research tasks) |

Set defaults in `[defaults]` and override them per task; `null` turns an
inherited validator off.

## Prompt additions

Files in `prompts/` are appended, as written, to the engine's base prompt of
the same name: `rules.md` for house style in every document, `critic.md` for
extra review checks, `summary.md` for the summary. They can add to the base
prompts but not replace them, and they are not templates.

## Check it

```bash
spadework playbooks show ./playbooks/market-scan
spadework check -C acme
```

`check` loads the playbook, validates every file, checks the task graph for
unknown ids and cycles, and renders every template, so most mistakes show up
here with a file and line. Try it with `SPADEWORK_EXECUTOR=fake spadework
run` before spending anything.

Have an idea for a playbook others could use? Open a *playbook idea* issue.
