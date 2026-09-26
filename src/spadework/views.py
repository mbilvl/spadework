"""How commands present their results, as text and as JSON."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spadework.console import Ui
from spadework.errors import SpadeworkError
from spadework.plan import Plan, PlannedTask, task_estimate_usd

if TYPE_CHECKING:
    from rich.table import Table

    from spadework.playbook import Playbook
    from spadework.project import Project
    from spadework.render import PublicView

# The version of every JSON document the CLI prints. Bump it on any change
# that could break a consumer.
JSON_SCHEMA_VERSION = 1

_STATE_STYLES = {
    "pending": "cyan",
    "fresh": "green",
    "stale": "yellow",
    "failed": "red",
    "invalid": "red",
    "pinned": "magenta",
}


def _table(*columns: str, right: tuple[str, ...] = ()) -> Table:
    from rich.table import Table

    table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0), header_style="bold")
    for column in columns:
        table.add_column(column, justify="right" if column in right else "left")
    return table


def _indented(table: Table) -> Any:
    from rich.padding import Padding

    return Padding(table, (0, 0, 0, 2), expand=False)


def usd(amount: float) -> str:
    return f"${amount:.2f}"


def _rounds(rounds: int) -> str:
    if rounds == 0:
        return "no review"
    return f"{rounds} review round" + ("" if rounds == 1 else "s")


def print_plan(ui: Ui, plan: Plan) -> None:
    from rich.text import Text

    limits = plan.limits
    ui.result(f"playbook  {plan.playbook} {plan.playbook_version}")
    budget = [usd(limits.budget_usd), limits.max_time, _rounds(limits.rounds)]
    ui.result(f"budget    {' · '.join(budget)}")
    ui.result()
    table = _table("task", "state", "reason", "est.", right=("est.",))
    for task in plan.tasks:
        table.add_row(
            task.id,
            Text(task.state, style=_STATE_STYLES.get(task.state, "")),
            task.reason or "",
            usd(task.estimate_usd) if task.will_run else "—",
        )
    ui.result(_indented(table))
    ui.result(
        f"estimated spend for pending and stale work: up to {usd(plan.estimate_usd)} "
        f"(cap {usd(limits.budget_usd)})"
    )


def _task_document(task: PlannedTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "output": task.output,
        "tier": task.tier,
        "model": task.model,
        "depends_on": list(task.depends_on),
        "research": task.has_research,
        "inputs": [file.display for file in task.inputs],
        "state": task.state,
        "reason": task.reason,
        "estimate_usd": task.estimate_usd,
    }


def check_document(
    project: Project,
    plan: Plan,
    warnings: list[dict[str, str]],
    public: PublicView | None,
) -> dict[str, Any]:
    limits = project.config.limits
    playbook = project.playbook
    document: dict[str, Any] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "ok": True,
        "project": {"name": plan.project},
        "playbook": {
            "name": playbook.name,
            "version": playbook.version,
            "source": playbook.ref.kind,
            "location": playbook.ref.spec,
        },
        "limits": {
            "budget_usd": limits.budget_usd,
            "max_time": limits.max_time,
            "max_time_s": limits.max_time_s,
            "rounds": limits.rounds,
            "session_timeout": limits.session_timeout,
            "session_timeout_s": limits.session_timeout_s,
        },
        "models": project.config.models.model_dump(),
        "inputs": [
            {"path": file.display, "bytes": file.size} for file in project.inputs
        ],
        "tasks": [_task_document(task) for task in plan.tasks],
        "estimate_usd": plan.estimate_usd,
        "warnings": warnings,
    }
    if public is not None:
        document["public"] = {
            "public_context": public.public_context,
            "research": [
                {"task": prompt.task.id, "questions": prompt.questions}
                for prompt in public.research
            ],
        }
    return document


def error_document(exc: SpadeworkError) -> dict[str, Any]:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "ok": False,
        "error": {
            "message": exc.message,
            "hint": exc.hint,
            "exit_code": int(exc.exit_code),
            "problems": [
                {"path": problem.path, "line": problem.line, "message": problem.message}
                for problem in exc.problems
            ],
        },
    }


def print_public(ui: Ui, view: PublicView) -> None:
    from rich.rule import Rule

    if not view.research:
        ui.result(
            "This playbook has no research tasks, so nothing from this project "
            "goes to a web-facing session."
        )
        return
    ui.info("Research sessions see this and nothing else from brief.md or inputs/.")
    ui.result(Rule("public_context", align="left", style="dim"))
    if view.public_context.strip():
        ui.result(view.public_context.rstrip("\n"))
    else:
        ui.result("(empty)", style="dim")
    for prompt in view.research:
        research = prompt.task.research
        assert research is not None
        ui.result()
        title = (
            f"{prompt.task.id}: research questions "
            f"(up to {research.max_searches} searches, {research.max_fetches} fetches)"
        )
        ui.result(Rule(title, align="left", style="dim"))
        ui.result(prompt.questions.rstrip("\n"))


def print_playbook_list(ui: Ui, playbooks: list[Playbook]) -> None:
    table = _table(
        "name", "version", "tasks", "source", "description", right=("tasks",)
    )
    for playbook in playbooks:
        source = (
            "built-in" if playbook.ref.kind == "built-in" else playbook.ref.display_root
        )
        table.add_row(
            playbook.name,
            playbook.version,
            str(len(playbook.tasks)),
            source,
            playbook.description,
        )
    ui.result(table)


def print_playbook(ui: Ui, playbook: Playbook) -> None:
    from rich.text import Text

    manifest = playbook.manifest
    defaults = manifest.defaults
    source = (
        "built-in" if playbook.ref.kind == "built-in" else playbook.ref.display_root
    )
    needs = ["title", "reader"]
    if playbook.research_tasks:
        needs.append("public_context")
    needs += [key for key in manifest.requires_brief if key not in needs]
    default_parts = [f"{defaults.tier} tier"]
    if defaults.budget_usd is not None:
        default_parts.append(f"{usd(defaults.budget_usd)} per draft")
    if defaults.max_lines is not None:
        default_parts.append(f"at most {defaults.max_lines} lines")
    prompts = ", ".join(f"prompts/{name}.md" for name in sorted(playbook.prompts))

    ui.result(f"{playbook.name} {playbook.version} · {source}", style="bold")
    ui.result(playbook.description)
    ui.result()
    ui.result(f"brief needs  {', '.join(needs)}")
    ui.result(f"defaults     {' · '.join(default_parts)}")
    ui.result(f"review       up to {manifest.review.max_findings} findings per round")
    ui.result(f"appends to   {prompts or 'nothing'}")
    ui.result()
    table = _table("task", "")
    table.show_header = False
    for task in playbook.tasks:
        details = [task.output, task.tier, f"est. {usd(task_estimate_usd(task))}"]
        if task.depends_on:
            details.append("after " + ", ".join(task.depends_on))
        text = Text.assemble(task.title, "\n", ("→ " + " · ".join(details), "dim"))
        if task.research is not None:
            research = task.research
            text.append(
                f"\nresearch: up to {research.max_searches} searches and "
                f"{research.max_fetches} fetches",
                style="dim",
            )
        table.add_row(task.id, text)
    ui.result(_indented(table))
