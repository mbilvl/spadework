# Security policy

spadework runs AI sessions unattended, and some of them read untrusted web
content. Its security model is part of the specification:
[07 — Security](docs/spec/07-security.md) sets out the threat model, the
invariants spadework keeps, and the risks it accepts.

## Supported versions

Security fixes go into the latest release only.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories:
[report a vulnerability](https://github.com/mbilvl/spadework/security/advisories/new),
or choose **Report a vulnerability** on the repository's **Security** tab.
Don't open a public issue, pull request, or discussion.

Please include:

- what the problem is, and what an attacker could achieve with it
- the version or commit you tested
- steps to reproduce. For a guard bypass, the tool call payload that got
  through is ideal.

Use fictional data in your report. Never include a real brief, real inputs,
or logs from a real project.

## What counts

Anything that breaks one of the
[invariants](docs/spec/07-security.md#invariants). For example:

- a session reading a file outside its staging directory, or writing to any
  path other than its one expected output
- a session reaching the control plane in `.spadework/`: policies,
  settings, counters, state, records, or manifests
- the brief or inputs reaching a session that has web access
- a session running code, delegating to a subagent, or using an MCP tool
- the guard allowing a call when it should fail closed
- a playbook running code, or reading files outside its own directory
- the source verifier reaching a loopback, private, or link-local address
- a run getting past its budget, time, or tool-call limits

If the root cause is in Claude Code itself, please report it to Anthropic as
well.

## Out of scope

- A compromised Claude Code binary or Python environment
- Protections the user has deliberately turned off
- The [residual risks](docs/spec/07-security.md#residual-risks) the spec
  accepts, such as search queries revealing terms from `public_context` to
  the search provider

## What happens next

A maintainer will confirm they have received your report, work with you on
a fix, and publish a GitHub security advisory when the fix is released. You
are credited in the advisory unless you'd rather not be.

Every fix for a sandbox or guard bypass adds a case to the security test
suite in `tests/security/`, so the same hole can't reopen.
