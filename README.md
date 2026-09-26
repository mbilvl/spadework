# spadework

**Evidence-first research for projects in discovery.**

Write a short brief about a project you're scoping. spadework runs a
playbook of research tasks as sandboxed Claude Code sessions, reviews its
own work, and hands you a short research pack: decisions, open questions,
risks, and findings where every external fact links to a source. When the
client answers a question, add the answer to `inputs/` and rerun. Only the
affected work is redone.

```bash
spadework init acme --playbook discovery-website
# edit acme/brief.md
spadework check -C acme      # validate + show the plan and estimated spend
spadework run -C acme        # research → draft → review → summary
open acme/out/00-summary.md
```

> **Status: design phase.** There is nothing to install yet. The
> specification and the v0 plan are open for feedback.

## Why

- **Sourced, not just plausible.** Research sessions return verbatim
  quotes, and spadework checks each quote against the live page. Anything
  it can't verify is labelled.
- **Safe to leave running.** Every session runs in its own sandbox: no
  shell, reads limited to its staged files, at most one file written.
  Sessions that browse the web never see your confidential brief.
- **Rerunnable.** Tasks are fingerprinted, the way `make` tracks targets.
  New inputs rerun only the tasks that depend on them.
- **Bounded.** Hard budgets on money, time and tool calls.
- **Reusable.** Domain knowledge lives in playbooks, which are plain data
  and safe to share.

## Documentation

- [How the docs are organised](docs/README.md)
- [Specification](docs/spec/00-overview.md): what spadework does and how
- [Decisions](docs/decisions/README.md): why it is built this way
- [Roadmap](docs/roadmap.md): the v0 plan

## Requirements (planned)

Python 3.11 or later, and [Claude Code](https://code.claude.com/docs)
installed and signed in. spadework runs your local `claude` CLI and never
handles credentials itself.

## License

[Apache-2.0](LICENSE)
