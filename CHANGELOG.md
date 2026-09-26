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
