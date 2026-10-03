# Changelog

All notable changes to spadework are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-10-03

The first release.

### Added

- `spadework init`, `check`, `run`, `status`, `logs`, `verify`, `clean`,
  `doctor` and `playbooks`. `run` researches and drafts each stale task as
  its own sandboxed Claude Code session, validates every document in code,
  repairs it once if needed, reviews the whole pack with a critic and fix
  sessions, writes a summary, and renders the pack into `out/`: one
  document per task with its sources, `00-summary.md`, `sources.md` and
  `pack.json`.
- Runs are bounded by money, time and tool calls, resume after an interrupt
  or a crash without redoing finished work, and redo only the tasks whose
  inputs changed, reusing web research. Documents you edit in `out/` are
  never overwritten silently.
- The sandbox: every session runs in a staging folder with only the files
  it needs, under a guard that allows only the tools its kind of session
  needs, at most one written file, and no shell, code or MCP tools, and
  that fails closed. Web-facing research sessions see only the brief's
  `public_context`. `doctor --canary` checks live that the sandbox holds on
  your machine.
- Source verification: each source's quote is checked against the live
  page, with strict protection against requests to private networks, and
  shown as a badge in the pack.
- The built-in `quickstart` and `discovery-website` playbooks, and a
  fictional example project in `examples/acme-fabrication/`. Playbooks are
  data (TOML, Markdown and sandboxed Jinja2), so you can write and share
  your own; see the guide.
- The `fake` executor (`SPADEWORK_EXECUTOR=fake`), to try every command
  without calling a model.
- JSON output for `check`, `status`, `verify` and `doctor`, NDJSON events
  for `run`, and JSON Schemas for the project files.
- Documentation: the specification, architecture decision records, the
  roadmap, and guides for getting started and writing a playbook.

[Unreleased]: https://github.com/mbilvl/spadework/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mbilvl/spadework/releases/tag/v0.1.0
