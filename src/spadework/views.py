"""How commands present their results, as text and as JSON."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spadework.console import Ui
from spadework.errors import SpadeworkError
from spadework.plan import SUMMARY_RESERVE_USD, Plan, PlannedTask, task_estimate_usd

if TYPE_CHECKING:
    from rich.table import Table

    from spadework.engine.events import RunFinished
    from spadework.engine.status import ProjectStatus
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


def plan_warnings(plan: Plan) -> list[tuple[str, str | None]]:
    """A warning for each task that could never start within the run budget."""
    cap = usd(plan.limits.budget_usd)
    reserve = usd(SUMMARY_RESERVE_USD)
    return [
        (
            f"task {task.id!r} can never start: its estimate "
            f"({usd(task.estimate_usd)}) plus the summary's reserve "
            f"({reserve}) is over the run budget ({cap})",
            "Raise limits.budget_usd in spadework.toml, or lower the task's "
            "budget_usd in the playbook.",
        )
        for task in plan.never_start()
    ]


def plan_tasks_document(plan: Plan) -> list[dict[str, Any]]:
    """The tasks of a plan, as ``run --json`` reports them."""
    return [{**_task_document(task), "will_run": task.will_run} for task in plan.tasks]


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
    warnings: list[dict[str, str | None]],
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


def _clock(seconds: float) -> str:
    whole = max(round(seconds), 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def print_run_finished(ui: Ui, event: RunFinished) -> None:
    """The end of a run: the result on stdout, what to do next on stderr."""
    sessions = f"{event.sessions} session" + ("" if event.sessions == 1 else "s")
    ui.result(
        f"run {event.run_id} · {event.outcome} · {usd(event.cost_usd)} of "
        f"{usd(event.budget_usd)} · {sessions} · {_clock(event.duration_s)}"
    )
    for path in event.rendered:
        ui.result(f"  wrote {path}")
    if event.guard_denials:
        denials = event.guard_denials
        calls = "1 tool call" if denials == 1 else f"{denials} tool calls"
        ui.warning(
            f"the sandbox blocked {calls} in this run",
            "See which with `spadework logs`.",
        )
    if event.possible_injection:
        ui.warning(
            "possible prompt injection encountered in the research for: "
            + ", ".join(event.possible_injection),
            "A web page may have tried to steer the session. Review those "
            "documents and their sources with extra care.",
        )
    message = event.message or event.outcome
    hint = event.hint or ""
    match event.outcome:
        case "complete":
            ui.info("Next: read the pack in out/. `spadework status` shows each task.")
        case "partial":
            ui.warning(message, hint)
        case "budget" | "time":
            ui.warning(f"stopped: {message}", hint)
        case "rate_limited":
            after = f"; resume after {event.resume_after}" if event.resume_after else ""
            ui.warning(f"stopped: {message}{after}", hint)
        case "interrupted":
            ui.err.print("interrupted", soft_wrap=True)
            if hint:
                ui.info(hint)
        case "error":
            ui.error(SpadeworkError(message, hint))


def _local(timestamp: str) -> str:
    """A manifest's UTC time, in local time to the minute."""
    import datetime as dt

    moment = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _active_text(status: ProjectStatus) -> str:
    active = status.active
    if active is None:
        return "no run is active"
    if active.owner is None:
        return ".spadework/lock exists but cannot be read"
    who = f"pid {active.owner.pid}, started {_local(active.owner.started_at)}"
    if active.alive is None:
        return f"a run on host {active.owner.host} holds the lock ({who})"
    if active.alive:
        return f"a run is active ({who})"
    return f"no run is active (a stopped run left its lock: {who})"


def _last_run_text(status: ProjectStatus) -> str:
    from spadework.engine.status import run_seconds

    last = status.last_run
    if last is None:
        return "none yet"
    outcome = last.outcome or (
        "did not finish" if status.last_run_unfinished else "running"
    )
    sessions = f"{last.totals.sessions} session" + (
        "" if last.totals.sessions == 1 else "s"
    )
    parts = [last.run_id, outcome, usd(last.totals.cost_usd), sessions]
    seconds = run_seconds(last)
    if seconds is not None:
        parts.append(_clock(seconds))
    parts.append(f"started {_local(last.started_at)}")
    if last.rate_limit_reset:
        parts.append(f"resets {_local(last.rate_limit_reset)}")
    return " · ".join(parts)


def print_status(ui: Ui, status: ProjectStatus) -> None:
    from rich.text import Text

    plan = status.planned.plan
    ui.result(f"project   {plan.project} · {plan.playbook} {plan.playbook_version}")
    ui.result(f"run       {_active_text(status)}")
    ui.result(f"last run  {_last_run_text(status)}")
    ui.result()
    table = _table("task", "state", "reason")
    for task in plan.tasks:
        table.add_row(
            task.id,
            Text(task.state, style=_STATE_STYLES.get(task.state, "")),
            task.reason or "",
        )
    ui.result(_indented(table))
    if status.last_run_unfinished:
        ui.info("The last run stopped without finishing. `spadework run` resumes it.")


def status_document(project: Project, status: ProjectStatus) -> dict[str, Any]:
    from spadework.engine.status import run_seconds

    plan = status.planned.plan
    active = status.active
    last = status.last_run
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "ok": True,
        "project": {"name": plan.project},
        "playbook": {"name": plan.playbook, "version": plan.playbook_version},
        "active_run": None
        if active is None
        else {
            "pid": None if active.owner is None else active.owner.pid,
            "host": None if active.owner is None else active.owner.host,
            "started_at": None if active.owner is None else active.owner.started_at,
            "alive": active.alive,
        },
        "last_run": None
        if last is None
        else {
            "run_id": last.run_id,
            "outcome": last.outcome,
            "finished": last.outcome is not None,
            "unfinished": status.last_run_unfinished,
            "started_at": last.started_at,
            "ended_at": last.ended_at,
            "duration_s": run_seconds(last),
            "cost_usd": last.totals.cost_usd,
            "sessions": last.totals.sessions,
            "guard_denials": last.totals.guard_denials,
            "rate_limit_reset": last.rate_limit_reset,
            "error": last.error,
        },
        "tasks": [
            {
                "id": task.id,
                "output": task.output,
                "state": task.state,
                "reason": task.reason,
            }
            for task in plan.tasks
        ],
        "warnings": list(status.warnings),
    }
