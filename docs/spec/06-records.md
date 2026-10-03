# 06 — Records, sources and verification

Status: Implemented (v0.1)

Every session ends with a **structured result**, which is JSON enforced by a
schema. Documents are for people to read. Records are for the engine, for
downstream sessions, and for anyone who wants the pack as data. This
replaces the prototype's free-text summary block, which had to be parsed
with regular expressions.

The schemas are pydantic models. The JSON Schema passed to the executor is
generated from them, and the engine validates the result again on its side.
If the result does not match the schema, the session is retried once with
the validation error. If it fails again, the session is `failed`.

## Result schemas

### `ResearchResult`

```json
{
  "sources": [
    {
      "id": "S1",
      "url": "https://cms.example/pricing",
      "title": "Pricing | ExampleCMS",
      "publisher": "ExampleCMS",
      "accessed": "2026-09-27",
      "quote": "The Free plan includes up to 20 editors … and up to 10,000 documents",
      "claim": "ExampleCMS's free plan allows 20 editors and 10k documents"
    }
  ],
  "notes": "Markdown findings. Every factual sentence cites [S#].",
  "unanswered": ["Self-hosted CMS pricing with EU data residency"]
}
```

- `quote` is a **verbatim** excerpt from the page of at most 300 characters.
  `…` may join up to 4 fragments of at least 3 words each, in page order. It
  is what verification checks.
- At most 30 sources per session.
- `unanswered` feeds into the draft's gaps. An honest "could not find" is a
  valid result.

### `DraftResult` (also returned by repair)

```json
{
  "status": "ok",
  "summary": "Recommends a static site + hosted headless CMS; low two-figure yearly running cost.",
  "key_facts": ["Stack: static site + hosted headless CMS", "Portuguese version: supported, adds ~30% effort"],
  "decisions": [
    { "id": "D1", "decision": "Hosted headless CMS", "reason": "Best editor UX for non-technical staff",
      "reversibility": "moderate", "alternatives": ["WordPress", "Self-hosted CMS"] }
  ],
  "open_questions": [
    { "id": "Q1", "question": "Is a Portuguese version required at launch?", "why": "Changes build effort and CMS setup",
      "answer_format": "yes_no" }
  ],
  "assumptions": ["No e-commerce in the first 12 months"],
  "risks": [
    { "risk": "Single maintainer", "likelihood": "medium", "impact": "high", "mitigation": "Documented handover" }
  ],
  "gaps": ["Could not confirm Payload Cloud regional pricing"]
}
```

| Field | Limit | Used by |
|---|---|---|
| `status` | `ok` \| `partial` | The task state |
| `summary` | 1 line | Live view, manifest |
| `key_facts` | ≤ 5 | Passed to dependent sessions. This replaces the prototype's ledger. |
| `decisions` | ≤ 8 | Summary, `pack.json` |
| `open_questions` | ≤ 6 | Summary (deduplicated), `pack.json` |
| `risks`, `assumptions`, `gaps` | ≤ 8 each | Summary, `pack.json` |

IDs are local to a task. In `pack.json`, they are qualified with the task id,
for example `stack:D1`.

| Field | Values |
|---|---|
| `summary` | One line, at most 300 characters |
| `decisions[].reversibility` | `easy` \| `moderate` \| `hard` |
| `open_questions[].answer_format` | `yes_no` \| `choice` \| `number` \| `date` \| `document` \| `text` |
| `risks[].likelihood`, `risks[].impact` | `low` \| `medium` \| `high` |

Every field in every result is required, even when it is empty (`[]` or
`""`), and no other fields are allowed: the schema passed to the executor is
strict and self-contained, and the engine validates the result again with
the same model. IDs are unique within a result.

### `FixResult`

`DraftResult` plus:

```json
{ "applied": ["F2", "F5"], "rejected": [{ "finding": "F3", "reason": "Would require inventing a price" }] }
```

### `CriticResult`

```json
{
  "verdict": "continue",
  "findings": [
    { "id": "F1", "target": "06-tech-stack.md", "kind": "contradiction", "severity": "high",
      "instruction": "Align the bilingual effort estimate with 02-audience.md (30% vs 'doubles')." }
  ],
  "biggest_gap": "No verified pricing for the recommended hosting in EUR."
}
```

`kind` is one of `fabrication`, `contradiction`, `filler`, `specificity`,
`priority`, `missing`, `length`, or `other`, and `severity` is `low`,
`medium` or `high`. `biggest_gap` is one line and may be empty. Findings are capped at the
playbook's `max_findings`. Every instruction must be something a fixer can
carry out with its own document and the brief, without inventing facts.

### `SummaryResult`

```json
{ "status": "ok", "open_questions": [ { "question": "…", "why": "…", "from": ["stack:Q1", "audience:Q2"] } ] }
```

This is the consolidated list of at most 10 open questions, each traced back
to the task questions it merges. `status` is `ok` or `partial`.

## Citations

- Documents cite sources inline as `[S1]`, `[S2]`, and so on. IDs are local
  to the task, so a document never copies another document's `[S#]`: a fact
  taken from another document is attributed to it by file name, for example
  *(see 01-landscape.md)*. A task without research cites no `[S#]` at all.
- A claim with no source is written as an inference ("likely", "we estimate")
  or tagged `[unverified]`.
- When rendering to `out/`, the engine appends a `## Sources` section to each
  document: a table of id, source (title linked to its URL, and publisher),
  access date and verification badge. It lists only the sources that
  document cites; a document without research, or citing none, says so, and
  cited ids missing from the research are named. Sources that are not cited
  stay in `pack.json`. Text from the web or a model is escaped before it is
  written into Markdown.
- `out/sources.md` lists every source, grouped by document: its URL,
  publisher, access date, badge with detail and check time, final URL if
  different, whether it is cited, and its claim and quote.

## Verification

This runs by default between each research session and its draft. It is
also available as `spadework verify`. It is plain code with no model
involved.

1. Fetch the URL. HTTP(S) only, with the safety rules from
   [07](07-security.md#i9-verification-fetches-cannot-reach-private-networks).
2. Extract the visible text: drop `head`, `script`, `style`, `nav`,
   `noscript`, `template`, `svg` and other content browsers don't show,
   including elements marked `hidden` or styled `display: none`, closed
   dialogs, and fallback content inside media elements; decode entities;
   separate block elements with a space; and collapse whitespace. Only
   character encodings from the WHATWG Encoding Standard are honoured. This
   step runs in a separate process with a hard time limit, so a hostile page
   cannot stall a run.
3. Normalise both texts: unify typographic quotes and dashes, Unicode NFKC,
   casefold, drop soft hyphens and zero-width characters, and collapse
   whitespace.
4. Split the quote into fragments on `…`, `...` or `[…]`. Each fragment must
   appear after the previous one. There is no fuzzy matching.

| Status | Meaning | Badge |
|---|---|---|
| `verified` | The quote was found | ✓ verified |
| `quote_not_found` | The page loaded, but the quote is not in it | ✗ quote not found |
| `unreachable` | Network error, 4xx or 5xx, a timeout, blocked by the rules of I9 (including special-use names such as `.example`), too many redirects or a loop, or not started before the run's time for verification ran out | ? unreachable |
| `unsupported` | Not HTML or text (PDF support is **[later]**), needs JavaScript to render, has no visible text, uses an unsupported encoding, is larger than 2 MB with the quote not in the first 2 MB, has more than 4 million characters of text with the quote not in the part read, or took too long to read. Also when the check would prove nothing: the quote appears in the URL (the page may just echo it), a fragment has fewer than 3 words or there are more than 4 fragments, or the page redirected to another site. | ? could not check |
| `unchecked` | Verification was turned off (`--no-verify` or `[verify] enabled = false`) | ? not checked |

Each result is stored with the research record as `{status, detail,
checked_at, final_url}`, and drafts receive each source's status and detail.
In a run, verification gets the time left once a draft could still start
(`max_time` minus the time used, minus 90 s); with none left, sources stay
`unchecked`. A source is treated as verified only when its status is
`verified`. An
`unchecked` source is checked by the next run that drafts its task with
verification on.

The draft session receives each source together with its status. Its rule is
that claims resting only on sources that are not `verified` must be tagged
`[unverified]`. A failed check is reported plainly. It is **not** treated as
proof of fabrication, since pages change and some sites block bots.

## `pack.json`

The whole pack as data, for scripts and for later phases:

```json
{
  "schema_version": 1,
  "project": "acme-fabrication",
  "generated_at": "2026-09-27T21:47:02Z",
  "versions": { "spadework": "0.1.0", "playbook": "discovery-website@0.1.0", "executor": "claude-cli@2.1.278" },
  "documents": [{ "task": "stack", "file": "06-tech-stack.md", "state": "fresh", "validators_ok": true }],
  "decisions": [], "open_questions": [], "consolidated_questions": [],
  "risks": [], "assumptions": [], "gaps": [],
  "sources": [{ "id": "stack:S1", "url": "…", "verification": "verified" }]
}
```

| Key | Each entry |
|---|---|
| `documents` | `{task, title, file, state, validators_ok, summary, key_facts}`, for every task, in file-name order |
| `decisions` | `{id, task, decision, reason, reversibility, alternatives}`, with ids like `stack:D1` |
| `open_questions` | `{id, task, question, why, answer_format}` |
| `consolidated_questions` | `{question, why, from}`; ids in `from` that name no task question are dropped |
| `risks` | `{task, risk, likelihood, impact, mitigation}` |
| `assumptions`, `gaps` | `{task, assumption}`, `{task, gap}` |
| `sources` | `{id, task, url, title, publisher, accessed, quote, claim, cited, verification, verification_detail, checked_at, final_url}` |

`versions` come from the run that wrote the summary, and `generated_at`
changes only when the rest of the file does.
