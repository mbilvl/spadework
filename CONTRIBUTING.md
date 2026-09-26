# Contributing to spadework

Thanks for your interest in spadework. The project is in its design phase,
and the [roadmap](docs/roadmap.md) shows what is being built next. Feedback
on the [specification](docs/spec/00-overview.md) is as welcome as code.

## Ways to contribute

- **Comment on the design.** Open an issue about anything in `docs/` that is
  unclear, wrong, or missing.
- **Pick up roadmap work.** Say which item you are taking in its issue
  before you start, so work isn't duplicated.
- **Propose a playbook.** Describe the research it would do in an issue
  first. Playbooks are data, not code
  ([ADR 0004](docs/decisions/0004-playbooks-are-data.md)).
- **Report a bug.** See [Reporting bugs](#reporting-bugs).

For anything larger than a small fix, open an issue and agree on the
approach before you write code.

## Development setup

You need Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and git.

You don't need Claude Code or an Anthropic account to develop spadework.
The test suite runs the whole engine against the fake executor
([05 — Executors](docs/spec/05-executors.md#fake-tests-and-demos)) and never
calls a model.

```bash
git clone https://github.com/YOUR-USERNAME/spadework.git
cd spadework
uv sync
uv run pre-commit install
uv run spadework --version
```

`uv sync` creates `.venv/` and installs spadework in editable mode with the
development tools. `pre-commit install` runs the linters on every commit.
To try the CLI without calling a model, set `SPADEWORK_EXECUTOR=fake`.

## Running the checks

| Check | Command |
|---|---|
| Tests, with coverage | `uv run pytest --cov` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type check (strict on `src/`) | `uv run mypy src` |
| Everything pre-commit runs | `uv run pre-commit run --all-files` |

CI runs lint, type checks and tests on Ubuntu and macOS, with Python 3.11
and 3.14.

- **Tests never call a model.** Use the fake executor: its scripted
  sessions, its recorded `stream-json` fixtures, and its injectable
  failures. CI never spends money on model calls.
- **Security tests stay green.** Every security invariant has a test in
  `tests/security/` ([07 — Security](docs/spec/07-security.md)). If you find
  a way around the guard, report it privately first (see
  [Security](#security)).

## The spec moves with the code

`docs/` is the documentation of record
([ADR 0006](docs/decisions/0006-living-spec.md)).
[docs/README.md](docs/README.md) explains how it is organised.

- **A pull request that changes behaviour updates `docs/spec/` in the same
  pull request.** Reviewers read the spec diff alongside the code diff.
- The spec describes current behaviour plus the target of the next release.
  Mark anything further out **[later]**.
- Use MUST, SHOULD and MAY ([RFC 2119](https://www.rfc-editor.org/rfc/rfc2119))
  only for public contracts: file formats, CLI behaviour, and security
  invariants.
- Write an [ADR](docs/decisions/README.md) when a decision is hard to
  reverse, constrains contributors, or weakens a security invariant.
  Accepted ADRs are not edited. A new ADR supersedes them.

## Conventions

- **Enforce in code, not in prompts.** Anything that matters, such as the
  sandbox, length limits, result schemas and budgets, is checked by the
  engine. Prompts explain the rules, but they are never the only defence.
- **Few dependencies.** Runtime dependencies are a small, deliberate set.
  Adding one needs a sentence of justification in the pull request
  ([ADR 0001](docs/decisions/0001-python-typer-cli.md)).
- **The guard uses the standard library only.** `spadework.sandbox.guard`
  runs once per tool call.
- **Keep `--help` and `--version` fast.** Import heavy modules lazily.
- **Playbooks stay data.** TOML, Markdown and sandboxed Jinja2 templates.
  A playbook cannot declare tools, hooks, executors, or Python code.

## Never commit client data

spadework exists to handle confidential client material, so this repository
holds public material only.

- No real client briefs, inputs, or generated packs. Every example uses the
  fictional *Acme Fabrication*.
- No personal notes, chat or agent transcripts, or internal planning. If it
  matters to the project, write it up as an issue, a spec change, or an ADR.
- No secrets of any kind.

If you run spadework on a real project, keep that project folder outside
your clone. `.spadework/` is gitignored, but `brief.md`, `inputs/` and `out/`
are not.

### Using AI coding assistants

Contributions made with AI coding assistants are welcome. You are
responsible for what you submit, and your sign-off covers it. The rules
above apply in full: don't commit transcripts, or files that hold your
private context.

## Sign your commits (DCO)

spadework uses the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a contributor licence agreement. By adding a `Signed-off-by`
line to a commit, you certify that you wrote the change, or otherwise have
the right to submit it under the project's license.

```bash
git commit -s -m "Add the fake executor"
```

This adds a line built from your git `user.name` and `user.email`:

```
Signed-off-by: Jane Doe <jane@example.com>
```

Every commit in a pull request must be signed off. To add a sign-off you
forgot:

```bash
git commit --amend -s --no-edit     # the last commit
git rebase --signoff main           # every commit on your branch
git push --force-with-lease
```

## Pull requests

- Keep each pull request to one change, and link the issue it resolves.
- Fill in the pull request template, including the "spec updated?" checkbox.
- Add an entry under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for
  anything a user would notice.
- CI must be green, and every commit signed off.

## Reporting bugs

Open an issue using the bug template. Include `spadework --version`, your
operating system and Python version, the command you ran, and what
happened. Don't paste your brief or inputs. Logs in `.spadework/` contain
both, so redact them before you attach them.

## Security

Don't report vulnerabilities in public issues. Report them privately through
GitHub Security Advisories, as described in [SECURITY.md](SECURITY.md).

## Code of Conduct

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

spadework is licensed under [Apache-2.0](LICENSE). Contributions are
accepted under the same license.
