# Roadmap

v0 ends with **0.1.0**, the first public release: an installable CLI that
takes a brief and produces a sandboxed, reviewed and sourced research pack
using the `discovery-website` playbook, and that reruns only what changed.

Each milestone ends with something you can actually run. Sizes are relative:
S is a few days, M about a week, L more than that, all part-time. When the
repository is on GitHub, each milestone becomes a GitHub milestone, and each
bullet becomes an issue.

```
M0 foundations ─▶ M1 project model ─▶ M2 sandbox + executor ─▶ M3 pipeline ─▶ M4 review + pack ─▶ M5 verification ─▶ 0.1.0
                                      ▲ spike can start during M1
```

## M0 — Repository foundations · S

- [x] `LICENSE` (Apache-2.0) and `NOTICE`
- [x] `README.md`: what it is, its status, and links to the spec
- [x] `CONTRIBUTING.md`: dev setup with uv, running the tests, the "spec in
      the same PR" rule, DCO sign-off (`git commit -s`), and the no-client-data rule
- [x] `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md` (private
      reporting through GitHub Security Advisories), `CHANGELOG.md` (Keep a Changelog)
- [x] `pyproject.toml` (hatchling), `uv.lock`, `src/spadework/` layout,
      `spadework --version`, and a Typer app skeleton
- [x] ruff for lint and format, mypy (strict on `src/`), pytest with coverage, pre-commit
- [x] CI with GitHub Actions: {ubuntu, macos} × {3.11, 3.14} running lint,
      type checks and tests. Dependabot for Actions and uv.
- [x] Issue templates (bug, feature, playbook idea) and a PR template with a
      "spec updated?" checkbox
- [x] `AGENTS.md`: a public, contributor-facing guide for people who
      contribute with AI coding assistants (commands, conventions, what never
      to commit)

**Done when** `uvx --from . spadework --version` works, CI is green on both
operating systems, and a fresh clone contains nothing but public material.

## M1 — Project model: `init`, `check`, playbooks · M

- [x] pydantic models for `spadework.toml`, the brief's front matter,
      `playbook.toml`, and task front matter. JSON Schemas are exported into
      the package.
- [x] Front-matter parsing using YAML `safe_load`. Errors point to a file and line.
- [x] Jinja2 `SandboxedEnvironment` rendering with `StrictUndefined`, and a
      separate public-only context for research questions
- [x] Playbook loader for built-in and local-path playbooks, and the DAG
      checks (cycles and unknown ids)
- [x] Commands: `init`, `check` (the plan table, `--json`, `--show-public`),
      `playbooks list/show`
- [x] Built-in playbooks: `quickstart`, and `discovery-website` generalised
      from the prototype's nine tasks
- [x] `examples/acme-fabrication/`: a fictional brief and inputs

**Done when** `spadework init demo && spadework check -C demo` prints a
plan; each kind of invalid brief or playbook produces an actionable error
that has a test; the models have at least 90% test coverage.

## M2 — Sandbox and executor: one task end to end · L

- [ ] **Spike first** (time-boxed to 2 days; it can run during M1). Answer
      the five open items in [05 — Executors](spec/05-executors.md), above
      all whether `--restricted` can run unattended, and write the answers
      back into the spec.
- [ ] Staging directories, per-session policy and settings generation, and the
      control directory kept outside the staging dirs
- [ ] `spadework.sandbox.guard`: standard library only, allowlist, path and
      pattern checks, exact writable paths, deadline, file-locked counters,
      denial log
- [ ] **Security test suite**: a table-driven test with guard payloads. It
      includes every hole found in the prototype (control-file writes, writes
      to other documents, `mcp__*`, absolute Glob and Grep patterns,
      exfiltration URLs), plus `..`, symlinks, case and Unicode path tricks.
- [ ] The `claude-cli` executor: the flag set, stream parsing, structured
      output, cost, rate-limit detection, and process-group timeouts
- [ ] The `fake` executor, with recorded `stream-json` fixtures and
      injectable failures
- [ ] `run --only TASK` for a single draft session, `logs`, and
      `doctor` with `--canary`

**Done when** a real draft session runs sandboxed and produces a validated
output and record; `doctor --canary` shows that a read outside the workdir
and a write to a second file are both denied; all guard tests pass; CI runs
a task end to end through the fake executor.

## M3 — Pipeline: DAG, cache, budgets, resumability · L

- [ ] Planner, draft and research keys, `state.json` (written atomically),
      and stale reasons
- [ ] The research → draft split, with `public_context` only in research sessions
- [ ] Built-in validators and the repair session
- [ ] Pinned outputs: detect human edits in `out/` and never overwrite them silently
- [ ] Budgets (per run and per session, with the summary's share reserved),
      `max_time`, session timeouts, and tool-call caps
- [ ] Rate limits: a clean stop with exit code 6, and `--wait-on-rate-limit`
- [ ] The lock, Ctrl-C handling, and a manifest written on every exit path
- [ ] `status`, the Rich live view, plain output for non-TTY, and `run --json` NDJSON events

**Done when** the `quickstart` playbook runs end to end for real; `kill -9`
in the middle of a run followed by `run` again resumes without redrafting
fresh tasks; adding a file to `inputs/` marks exactly the matching tasks
stale, with the reason shown in `check`; hitting the budget exits with code
5 and leaves a valid manifest.

## M4 — Review loop, summary, rendering · M

- [ ] Critic and fix sessions with their schemas; validator failures added
      as findings; rejected findings recorded in the manifest
- [ ] The summary session plus the code-rendered status table and footer
- [ ] Rendering to `out/`: a `## Sources` section per document,
      `sources.md`, and `pack.json`

**Done when** a `discovery-website` run on the example project produces a
complete pack within the default $6 budget; the critic's verdict ends the
loop; no document breaks its validators without being reported.

## M5 — Source verification · M

- [ ] An httpx verifier with SSRF protections: DNS checked on every
      redirect, size and time caps, and no credentials sent
- [ ] Automatic verification between research and draft; `spadework verify`;
      badges in the documents; counts in the summary
- [ ] Writers are told each source's verification status, and must tag
      claims that rest only on sources that aren't verified

**Done when** the SSRF tests pass (private IPs, redirects to private IPs,
non-HTTP schemes, oversized responses); verification statuses appear in
the pack; a planted fake quote is reported as `quote_not_found`.

## Release 0.1.0 · S

- [ ] `docs/guides/`: getting started, and writing a playbook
- [ ] Every spec status updated to `Implemented (v0.1)`; the CHANGELOG
- [ ] Publish to PyPI through GitHub Actions trusted publishing (OIDC, no
      stored tokens), with build attestations
- [ ] Dogfood: run a real, private project through spadework and compare it
      with its hand-assisted pack. Findings become issues, not committed documents.

**Done when** someone new goes from `uvx spadework init` to a pack by
following the README alone.

## After v0 (roughly in order of value)

1. **Revise mode.** When inputs change, update the existing documents from a
   diff instead of redrafting them. This is how a human handles new client
   answers.
2. **The open-question loop.** `spadework questions export` produces a list
   to send the client. Their answers map back to question ids, and only the
   tasks those questions block are invalidated.
3. **Phase chaining.** A playbook starts from a previous pack's
   `pack.json`, for example a build-planning phase that follows discovery.
4. **Evals.** Fixture briefs and scoring (validator pass rate, share of
   verified sources, contradictions found, cost), so that prompt changes are
   measured rather than guessed.
5. **An `agent-sdk` executor**, with the guard as an in-process function.
6. **Parallel sessions** for independent tasks.
7. **More playbooks** (market scan, technical due diligence, RFP response),
   plus loading playbooks from git.
8. PDF inputs, a maximum age for research results, a user-level config
   file, and Windows support.

## Open design questions

| # | Question | Resolve by |
|---|---|---|
| 1 | Which flag set is strictest while still unattended (`--restricted`, and the permission mode)? | M2 spike |
| 2 | Should inputs marked `public: true` be allowed into research sessions? | M3, after dogfooding |
| 3 | How far should the summary be written by code rather than by the model? | M4 |
| 4 | Is 300 characters the right quote length, and do we need fuzzy matching beyond ordered fragments? | M5, using real pages |
| 5 | How should packs be written in a language other than English? `language` exists, but the prompts are English. | After v0 |
