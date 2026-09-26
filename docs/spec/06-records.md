# 06 — Records, sources and verification

Status: Draft · Target: v0.1

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
      "quote": "Free plan … up to 20 editors … 10,000 documents",
      "claim": "ExampleCMS's free plan allows 20 editors and 10k documents"
    }
  ],
  "notes": "Markdown findings. Every factual sentence cites [S#].",
  "unanswered": ["Self-hosted CMS pricing with EU data residency"]
}
```

- `quote` is a **verbatim** excerpt from the page of at most 300 characters,
  with `…` allowed between fragments. It is what verification checks.
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
  to the task.
- A claim with no source is written as an inference ("likely", "we estimate")
  or tagged `[unverified]`.
- When rendering to `out/`, the engine appends a `## Sources` section to each
  document. It lists only the sources that document cites, each with its
  title, URL, access date and verification badge. Sources that are not cited
  stay in `pack.json`.
- `out/sources.md` lists every source, grouped by document.

## Verification

This runs by default between each research session and its draft. It is
also available as `spadework verify`. It is plain code with no model
involved.

1. Fetch the URL. HTTP(S) only, with the safety rules from
   [07](07-security.md#i9-verification-fetches-cannot-reach-private-networks).
2. Extract the visible text: drop `script`, `style` and `nav`, and collapse
   whitespace.
3. Normalise both texts: Unicode NFKC, casefold, unify quotes and dashes,
   and collapse whitespace.
4. Check that each fragment of the quote (split on `…`) appears in order.

| Status | Meaning | Badge |
|---|---|---|
| `verified` | The quote was found | ✓ verified |
| `quote_not_found` | The page loaded, but the quote is not in it | ✗ quote not found |
| `unreachable` | Network error, 4xx or 5xx, or a timeout | ? unreachable |
| `unsupported` | Not HTML or text (PDF support is **[later]**), or needs JavaScript to render | ? could not check |

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
