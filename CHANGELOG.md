# Changelog

All notable changes to spadework are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- The design documentation: the v0 specification, architecture decision
  records 0001–0007, and the roadmap.
- The Apache-2.0 license, a contributing guide, a code of conduct, and a
  security policy.
- The `spadework` command, with `init`, `check` (including `--json` and
  `--show-public`), `playbooks list` and `playbooks show`, and the global
  options `-C`, `-q`, `-v` and `--no-color`.
- The built-in `quickstart` and `discovery-website` playbooks, and a
  fictional example project in `examples/acme-fabrication/`.
- JSON Schemas for `spadework.toml`, the brief's front matter,
  `playbook.toml` and task front matter, shipped in the package.
- `spadework run`: research and draft each stale task as its own sandboxed
  Claude Code session, validate the output, repair it once if needed, and
  render the documents into `out/`. Runs are bounded by money and time,
  resumable after an interrupt or a crash, and redo only the tasks whose
  inputs changed. Live view on a terminal, plain lines otherwise, NDJSON
  with `--json`.
- `spadework status`, `spadework logs` and `spadework doctor`, including
  `doctor --canary`, a live check that the sandbox holds on your machine.
- The sandbox: a per-session guard that allows only the tools each kind of
  session needs, confines reads to the session's staged files, allows
  exactly one written file, keeps web-facing sessions away from anything
  but `public_context`, and fails closed.
- The `fake` executor (`SPADEWORK_EXECUTOR=fake`) for trying the CLI and for
  tests, without calling a model.
