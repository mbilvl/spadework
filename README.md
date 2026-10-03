# spadework

**Evidence-first research for projects in discovery.**

Write a short brief about a project you're scoping. spadework runs a
playbook of research tasks as sandboxed Claude Code sessions, reviews its
own work, and hands you a short research pack: decisions, open questions,
risks, and findings where every external fact links to a source. When the
client answers a question, add the answer to `inputs/` and rerun: the work
that depends on it is redone, and the web research is reused.

```bash
spadework init acme --playbook discovery-website
# edit acme/brief.md
spadework check -C acme      # validate, and show the plan and the estimated spend
spadework run -C acme        # research → draft → review → summary
open acme/out/00-summary.md
```

> **Status: 0.1.0, the first release.** Every part is built and tested,
> including the sandbox against the real Claude Code CLI, but real-world use
> is just beginning: please report what you find. The pack it writes is a
> starting point for your own judgement, not a finished report.

## Why

- **Sourced, not just plausible.** Research sessions return verbatim
  quotes, and spadework checks each quote against the live page. Anything
  it can't verify is labelled.
- **Safe to leave running.** Every session runs in its own sandbox: no
  shell, reads limited to its staged files, at most one file written.
  Sessions that browse the web never see your confidential brief.
- **Rerunnable.** Tasks are fingerprinted, the way `make` tracks targets.
  A change reruns only the tasks that depend on it.
- **Bounded.** Hard limits on money, time and tool calls.
- **Reusable.** Domain knowledge lives in playbooks, which are plain data
  and safe to share.

## Install

spadework runs on macOS and Linux. You need Python 3.11 or later, and
[Claude Code](https://code.claude.com/docs) 2.1.278 or later, installed and
signed in: `claude` must work in your terminal. spadework runs your local
`claude` and never handles credentials itself.

```bash
uvx spadework --help          # run it without installing
pipx install spadework        # or install it
```

The commands below assume it is installed; with `uvx`, type
`uvx spadework` where they say `spadework`. To run the latest code from this
repository instead:

```bash
uv tool install git+https://github.com/mbilvl/spadework
```

## Quick start

```bash
spadework doctor                          # check your setup
spadework init acme --playbook quickstart # a small, cheap playbook to start with
```

Fill in `acme/brief.md`. The comments in it explain each field. Then:

```bash
spadework check -C acme --show-public     # what web-facing sessions will see
spadework run -C acme
```

The pack lands in `acme/out/`: `00-summary.md`, one document per task (each
with its sources and their verification results), `sources.md` and
`pack.json`. A run stops at its budget (`$6.00` by default) or time limit
(`2h`), and the next `run` carries on from where it stopped.

To try the commands without calling a model or spending anything, set
`SPADEWORK_EXECUTOR=fake`.

[Getting started](https://github.com/mbilvl/spadework/blob/main/docs/guides/getting-started.md) walks through all of it,
and [writing a playbook](https://github.com/mbilvl/spadework/blob/main/docs/guides/writing-a-playbook.md) shows how to
make your own.

## Documentation

- [Guides](https://github.com/mbilvl/spadework/blob/main/docs/guides/getting-started.md): getting started, writing a playbook
- [Specification](https://github.com/mbilvl/spadework/blob/main/docs/spec/00-overview.md): what spadework does and how
- [Decisions](https://github.com/mbilvl/spadework/blob/main/docs/decisions/README.md): why it is built this way
- [Roadmap](https://github.com/mbilvl/spadework/blob/main/docs/roadmap.md): what's next
- [How the docs are organised](https://github.com/mbilvl/spadework/blob/main/docs/README.md)

## Contributing

Issues and pull requests are welcome; start with
[CONTRIBUTING.md](https://github.com/mbilvl/spadework/blob/main/CONTRIBUTING.md). Report security problems privately, as
described in [SECURITY.md](https://github.com/mbilvl/spadework/blob/main/SECURITY.md).

## License

[Apache-2.0](https://github.com/mbilvl/spadework/blob/main/LICENSE)
