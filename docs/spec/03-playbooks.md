# 03 — Playbooks

Status: Draft · Target: v0.1

A playbook is a reusable research recipe: a list of tasks, the rules for
writing them, the review rubric, and summary instructions. **Playbooks are
data. They cannot execute code** ([ADR 0004](../decisions/0004-playbooks-are-data.md)),
so it is safe to use a playbook written by someone else.

## Layout

```
discovery-website/
├── playbook.toml
├── brief.template.md       # copied by `spadework init`
├── tasks/
│   ├── scope.md
│   ├── references.md
│   └── …
└── prompts/                # optional; each file is APPENDED to the engine's base prompt
    ├── rules.md            # house style for this playbook
    ├── critic.md           # extra review tests
    └── summary.md          # extra summary instructions
```

A playbook can **append** to the engine's base prompts but cannot replace
them. The base prompts carry the truth rules, the citation format and the
output contract, and those must stay intact. Each file in `prompts/` must be
named after a base prompt (`rules`, `research`, `draft`, `repair`, `fix`,
`critic` or `summary`, plus `.md`) and is appended as written. It is not
rendered as a template, so it cannot pull brief content into a research
session. `brief.template.md` is optional for loading a playbook and required
by `init`.

## `playbook.toml`

```toml
schema_version = 1
name = "discovery-website"
version = "0.1.0"                    # part of every task fingerprint
description = "Discovery pack for a new marketing or catalogue website."
requires_brief = ["priorities"]      # brief fields this playbook needs, on top of the base ones

[defaults]                           # task-level defaults, which tasks can override
tier = "draft"
budget_usd = 1.00
max_lines = 250

[review]
max_findings = 12
```

## Task files

The task id is the filename without `.md`. It must match `[a-z][a-z0-9-]*`.
The front matter is YAML, and the body is a Jinja2 template.

```markdown
---
title: Tech stack, CMS and hosting
output: 06-tech-stack.md            # filename in out/
depends_on: [ia, scope]             # task ids
tier: review                        # draft | review → model via spadework.toml [models]
inputs: ["**"]                      # globs over inputs/; default "**"
budget_usd: 1.50
research:                           # optional → a research session runs before the draft
  max_searches: 6
  max_fetches: 8
  questions: |
    For a {{ public_context }}:
    - current pricing and free-tier limits of Sanity, Payload and managed WordPress hosts
    - image CDN options with edge locations in southern Europe
validate:
  max_lines: 250
  required_headings: [Recommendation, Options, Cost, Risks, Open questions]
---
The hard requirement: non-technical staff must add and edit catalogue items
unaided. Optimise for that, for a photo-heavy catalogue that stays fast on
mobile, and for low running cost.

{% if brief.currencies %}Give costs in {{ brief.currencies | join(" and ") }}.{% endif %}
...
```

| Field | Required | Default | Notes |
|---|---|---|---|
| `title` | yes | — | |
| `output` | yes | — | A file name ending in `.md`, unique within the playbook ignoring case, and neither `00-summary.md` nor `sources.md` |
| `depends_on` | no | `[]` | Unknown ids and cycles are errors in `check` |
| `tier` | no | `defaults.tier` | |
| `inputs` | no | `["**"]` | Which inputs are staged, and which inputs make the task stale |
| `budget_usd` | no | `defaults.budget_usd` | Used as the cost estimate and the per-session cap ([04](04-engine.md)) |
| `research` | no | none | Adds a research session with web access and **public context only**. `max_searches` defaults to 6 and `max_fetches` to 8; each is 0–80 and they cannot both be 0. |
| `validate` | no | `defaults` | Built-in validators, see below. Keys override `[defaults]` one by one, and `null` turns an inherited validator off (for `forbidden_phrases`, it drops only the playbook's additions). |

## Templates

Templates are rendered with Jinja2's `SandboxedEnvironment` and
`StrictUndefined`, so a missing variable is an error in `check` rather than a
silently empty string. Autoescaping is off, and `trim_blocks`,
`lstrip_blocks` and `keep_trailing_newline` keep Jinja2's defaults (off, off,
on). Templates cannot touch the filesystem outside the playbook folder.
`check` renders every template, using placeholders for values that exist
only during a run.

| Rendered as | Variables |
|---|---|
| Task body (draft, repair, fix) | `brief` (fields, `extra`, `body`), `task`, `project`, `files` (the staged files, each with its first heading), `deps` (per dependency: `output`, `key_facts`), `research` (notes and sources, when present) |
| `research.questions` | `public_context`, `task`, `brief.language`. **Nothing else.** This is enforced by the render context, not by convention. |

| Variable | Shape |
|---|---|
| `brief` | The front-matter fields, `extra` (every other key), and `body` |
| `task` | `id`, `title`, `output`, `tier`, `depends_on`, `inputs`, `has_research` |
| `project` | `name` |
| `files` | A list of `{path, heading}` for the staged files |
| `deps` | A mapping from dependency id to `{id, title, output, key_facts}` |
| `research` | `{notes, sources, unanswered}`, each source carrying its verification status; `None` for a task without research |

Every variable is read-only, and keys win over method names, so
`brief.extra.items` is the brief's `items` field. The engine's base draft
prompt already lists the staged files, each dependency's key facts, and the
research notes, so task templates need not repeat them.

## Engine base prompts

These ship with spadework and are versioned together. A change to them is a
change to every fingerprint.

| Prompt | Carries |
|---|---|
| `rules` | The truth rules (cite sources as `[S#]`; label inferences; tag unsourced claims `[unverified]`; never invent names, numbers or URLs), plus: write the current truth and not revision history, no preamble, prefer tables to long prose, start with an H1 and a one-line purpose, and the reader from `brief.reader` |
| `research` | How to search, what counts as a good quote, how many sources to aim for, and what to report when something can't be found |
| `draft`, `repair`, `fix` | The output file and the result contract. A fixer may reject a finding if it says why. |
| `critic` | The generic rubric: fabrication, contradictions between documents, filler, length, specificity to *this* brief, the brief's priorities, and process narration |
| `summary` | The structure of `00-summary.md` |

The sandbox rules are **not** written into prompts as instructions to obey.
Prompts describe the environment ("you can write only `06-tech-stack.md`"),
and the guard enforces it.

## Built-in validators

Validators run in code after every draft, repair and fix session.

| Validator | Parameter | Checks |
|---|---|---|
| `max_lines` / `min_lines` | int | Line count |
| `starts_with_h1` | bool, default `true` | The first non-blank line is `# …` |
| `required_headings` | list | Each name appears as a heading at any level, case-insensitive |
| `forbidden_phrases` | list | Added to the engine defaults: "lorem ipsum", "as an AI", "in this document we will", "this session", "session 2" |
| `max_open_questions` | int, default 6 | Counted from the record, not the text |
| `citations_resolve` | bool, default on for research tasks | Every `[S#]` in the document exists in that task's sources |
| `min_sources` | int | Research tasks only (an error on any other task). Counts the distinct sources the document cites. |

When a validator fails, the engine runs **one** repair session with the
failure messages. The exception is `min_sources` when the research found
fewer sources than required: repair cannot add sources, so the task is
marked `invalid` straight away. If validation still fails, the task is marked `invalid`.
The pack is still produced, and `status`, the summary, and the exit code (`4`)
all report the failure.

Custom validators written in Python are **[later]** and will need explicit
opt-in, because they are code ([ADR 0004](../decisions/0004-playbooks-are-data.md)).

## Loading

| `playbook =` | Resolves to |
|---|---|
| `"discovery-website"` | A built-in playbook shipped in the package |
| `"./playbooks/mine"` | A directory relative to the project |
| `"git+https://…"` | **[later]** |

## Built-in playbooks (v0)

**`quickstart`**: two small, cheap tasks. For trying spadework out and for
maintainers' live smoke runs.

| Task | Research | Produces |
|---|---|---|
| `landscape` | yes | Five comparable products or companies and what they show |
| `questions` | no | The ten questions to ask the client next. Depends on `landscape`. |

**`discovery-website`**: generalised from the original nine-task pack. All
client-specific detail now comes from the brief.

| Task | Research | Depends on |
|---|---|---|
| `scope` — scope, assumptions, success criteria, phasing | no | — |
| `audience` — audiences, trust signals, positioning, language | no | scope |
| `references` — regional peers, best-in-class references, patterns | yes | audience |
| `ia` — sitemap, page plans, content model, taxonomy, URLs | no | scope, audience |
| `design` — art direction, palette, type, photography, components | no | audience, references |
| `stack` — recommendation, options, image pipeline, hosting, total cost | yes | ia, scope |
| `content` — asset checklist, shot list, templates, blog plan | no | ia, design |
| `seo` — keywords, structured data, local presence, legal pages | yes | ia |
| `roadmap` — phases, calendar, decision log, risks, next steps | no | all of the above |
