"""The built-in validators, run in code after every draft, repair and fix.

See docs/spec/03-playbooks.md, Built-in validators. Each failure message says
what is wrong and what to change, because it is shown to the repair session
as well as to the user.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import SerializerFunctionWrapHandler, model_serializer

from spadework.models import StrictModel
from spadework.playbook import Validators
from spadework.records import DraftResult, Source


class ValidatorResult(StrictModel):
    """One validator's verdict on one document."""

    name: str
    ok: bool
    message: str | None = None
    """What failed and how to fix it. None when the validator passed."""
    detail: str | None = None
    """The failure in a few words, such as ``312 > 250``, for status lines."""
    repairable: bool = True
    """False when a repair session cannot fix the failure, such as
    ``min_sources`` when the research found too few sources."""

    @model_serializer(mode="wrap")
    def _compact(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # A pass is {"name", "ok"}, as in the run manifest's example.
        data: dict[str, Any] = handler(self)
        if self.message is None:
            data.pop("message", None)
        if self.detail is None:
            data.pop("detail", None)
        if self.repairable:
            data.pop("repairable", None)
        return data


def _pass(name: str) -> ValidatorResult:
    return ValidatorResult(name=name, ok=True)


def _fail(
    name: str, message: str, detail: str, *, repairable: bool = True
) -> ValidatorResult:
    return ValidatorResult(
        name=name, ok=False, message=message, detail=detail, repairable=repairable
    )


def failure_summary(results: Iterable[ValidatorResult]) -> str | None:
    """The failed validators in one line, with their specifics, such as
    ``validators failed: max_lines (312 > 250), required_headings (missing:
    Cost)``. None when none failed."""
    failed = [
        f"{result.name} ({result.detail})" if result.detail else result.name
        for result in results
        if not result.ok
    ]
    return f"validators failed: {', '.join(failed)}" if failed else None


def run_validators(
    text: str,
    validators: Validators,
    *,
    result: DraftResult,
    sources: Sequence[Source] = (),
) -> list[ValidatorResult]:
    """Check a document against a task's validators.

    ``result`` is the session's record (a ``FixResult`` is one too) and
    ``sources`` are the task's research sources. Only the validators that
    are on appear in the list, in the order of the spec's table.
    """
    lines = _lines(text)
    results = []
    if validators.max_lines is not None:
        results.append(_max_lines(lines, validators.max_lines))
    if validators.min_lines is not None:
        results.append(_min_lines(lines, validators.min_lines))
    if validators.starts_with_h1:
        results.append(_starts_with_h1(lines))
    if validators.required_headings:
        results.append(_required_headings(lines, validators.required_headings))
    results.append(_forbidden_phrases(lines, validators.forbidden_phrases))
    if validators.max_open_questions is not None:
        results.append(_max_open_questions(result, validators.max_open_questions))
    cited = cited_sources(text)
    known = [source.id for source in sources]
    if validators.citations_resolve:
        results.append(_citations_resolve(cited, known))
    if validators.min_sources is not None:
        results.append(_min_sources(cited, known, validators.min_sources))
    return results


def failures(results: Iterable[ValidatorResult]) -> list[ValidatorResult]:
    return [result for result in results if not result.ok]


def can_repair(results: Iterable[ValidatorResult]) -> bool:
    """Whether a repair session is worth running.

    Not when every validator passed, and not when any failure is one a
    repair cannot fix: the task is then invalid straight away.
    """
    failed = failures(results)
    return bool(failed) and all(result.repairable for result in failed)


def _lines(text: str) -> list[str]:
    return text.removeprefix("﻿").splitlines()


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _max_lines(lines: Sequence[str], limit: int) -> ValidatorResult:
    if len(lines) <= limit:
        return _pass("max_lines")
    return _fail(
        "max_lines",
        f"The document has {len(lines)} lines; the limit is {limit}. Cut at least "
        f"{_plural(len(lines) - limit, 'line')}: remove repetition, filler and "
        "restated context, and prefer tables to long prose.",
        f"{len(lines)} > {limit} lines",
    )


def _min_lines(lines: Sequence[str], limit: int) -> ValidatorResult:
    if len(lines) >= limit:
        return _pass("min_lines")
    return _fail(
        "min_lines",
        f"The document has {_plural(len(lines), 'line')}; it needs at least "
        f"{limit}. Cover what the task asks for in full, without padding.",
        f"{len(lines)} < {limit} lines",
    )


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")


def _headings(lines: Sequence[str]) -> list[tuple[int, int, str]]:
    """Each ATX heading outside code fences: (line number, level, text)."""
    found = []
    fence: str | None = None
    for number, line in enumerate(lines, start=1):
        if match := _FENCE.match(line):
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None and (match := _ATX.match(line)):
            text = re.sub(r"[ \t]+#+$", "", match.group(2) or "").strip()
            if text.strip("#"):
                found.append((number, len(match.group(1)), text))
    return found


def _starts_with_h1(lines: Sequence[str]) -> ValidatorResult:
    first = next(((n, line) for n, line in enumerate(lines, 1) if line.strip()), None)
    if first is None:
        return _fail(
            "starts_with_h1",
            "The document is empty. It must start with a level-1 heading, "
            "such as '# Tech stack'.",
            "empty document",
        )
    number, line = first
    headings = _headings(lines[:number])
    if headings and headings[0][:2] == (number, 1):
        return _pass("starts_with_h1")
    return _fail(
        "starts_with_h1",
        f"The first line must be a level-1 heading, such as '# Tech stack', but "
        f"line {number} is {_shorten(line.rstrip())!r}. Start the document with "
        "'# ' and its title, with nothing before it.",
        f"line {number} is not a level-1 heading",
    )


_NUMBERING = re.compile(r"^(?:\d+(?:\.\d+)*|[A-Za-z])[.)]\s+|^\d+(?:\.\d+)+\s+")


def _normalise_heading(text: str) -> str:
    text = re.sub(r"[*_`]", "", text)
    text = " ".join(text.split()).casefold()
    text = _NUMBERING.sub("", text)
    return text.rstrip(" :.")


def _heading_matches(heading: str, name: str) -> bool:
    # "Cost", "3. Cost", "Cost:", "Cost (EUR)" and "Cost - yearly" (or an
    # em or en dash) all count as the heading "Cost"; "Costs" and "Cost
    # model" do not.
    if heading == name:
        return True
    return any(
        heading.startswith(name + separator)
        for separator in (":", " (", " \u2014", " \u2013", " - ")
    )


def _required_headings(lines: Sequence[str], names: Sequence[str]) -> ValidatorResult:
    present = [_normalise_heading(text) for _, _, text in _headings(lines)]
    missing = [
        name
        for name in names
        if not any(_heading_matches(h, _normalise_heading(name)) for h in present)
    ]
    if not missing:
        return _pass("required_headings")
    listed = ", ".join(repr(name) for name in missing)
    return _fail(
        "required_headings",
        f"Missing required {'heading' if len(missing) == 1 else 'headings'}: "
        f"{listed}. Add each as a Markdown heading with that name, such as "
        f"'## {missing[0]}'. Any level counts, and case is ignored.",
        _shorten("missing: " + ", ".join(missing)),
    )


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = phrase.split()
    body = r"\s+".join(re.escape(word) for word in words)
    # Whole words only where the phrase starts or ends with a word character,
    # so "as an AI" does not match "as an AIM".
    start = r"(?<!\w)" if re.match(r"\w", words[0]) else ""
    end = r"(?!\w)" if re.search(r"\w$", words[-1]) else ""
    return re.compile(start + body + end, re.IGNORECASE)


def _forbidden_phrases(lines: Sequence[str], phrases: Sequence[str]) -> ValidatorResult:
    text = "\n".join(lines)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    found = []
    matched: list[tuple[str, list[int]]] = []
    seen: set[str] = set()
    for phrase in phrases:
        if not phrase.split() or phrase.casefold() in seen:
            continue
        seen.add(phrase.casefold())
        numbers = sorted(
            {
                bisect.bisect_right(starts, match.start())
                for match in _phrase_pattern(phrase).finditer(text)
            }
        )
        if numbers:
            where = ", ".join(str(n) for n in numbers[:5])
            if len(numbers) > 5:
                where += ", …"
            label = "line" if len(numbers) == 1 else "lines"
            found.append(f"{phrase!r} ({label} {where})")
            matched.append((repr(phrase), numbers))
    if not found:
        return _pass("forbidden_phrases")
    return _fail(
        "forbidden_phrases",
        f"Forbidden {'phrase' if len(found) == 1 else 'phrases'} found: "
        f"{'; '.join(found)}. Rewrite those sentences without them: the document "
        "states findings for the reader and does not describe how it was written.",
        _shorten(", ".join(phrase for phrase, _ in matched)),
    )


def _max_open_questions(result: DraftResult, limit: int) -> ValidatorResult:
    count = len(result.open_questions)
    if count <= limit:
        return _pass("max_open_questions")
    return _fail(
        "max_open_questions",
        f"The result lists {_plural(count, 'open question')}; the limit for this "
        f"task is {limit}. Keep the {limit} whose answers would change a decision "
        "most, merge related ones, and drop the rest from the result.",
        f"{count} > {limit}",
    )


_CITATION = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]")


def cited_sources(text: str) -> list[str]:
    """The source ids a document cites, in order of first use.

    Reads ``[S1]`` and grouped forms such as ``[S1, S3]``.
    """
    cited: dict[str, None] = {}
    for match in _CITATION.finditer(text):
        for source_id in re.split(r"\s*[,;]\s*", match.group(1)):
            cited.setdefault(source_id, None)
    return list(cited)


def _id_list(ids: Sequence[str]) -> str:
    return ", ".join(ids)


def _citations_resolve(cited: Sequence[str], known: Sequence[str]) -> ValidatorResult:
    unknown = [source_id for source_id in cited if source_id not in known]
    if not unknown:
        return _pass("citations_resolve")
    brackets = ", ".join(f"[{source_id}]" for source_id in unknown)
    have = (
        f"this task's sources are {_id_list(known)}"
        if known
        else "this task has no sources"
    )
    return _fail(
        "citations_resolve",
        f"The document cites {brackets}, but {have}. Cite only sources that "
        "exist. Rewrite a claim with no source as an inference, or tag it "
        "[unverified].",
        _shorten("unknown: " + ", ".join(unknown)),
    )


def _min_sources(
    cited: Sequence[str], known: Sequence[str], minimum: int
) -> ValidatorResult:
    if len(known) < minimum:
        return _fail(
            "min_sources",
            f"The research found {_plural(len(known), 'source')}, and this task "
            f"needs at least {minimum} cited. A repair cannot add sources: run "
            "again with --refresh-research, or lower validate.min_sources.",
            f"{len(known)} found < {minimum}",
            repairable=False,
        )
    used = [source_id for source_id in cited if source_id in known]
    if len(used) >= minimum:
        return _pass("min_sources")
    unused = [source_id for source_id in known if source_id not in used]
    return _fail(
        "min_sources",
        f"The document cites {_plural(len(used), 'distinct source')}"
        f"{f' ({_id_list(used)})' if used else ''}; it needs at least {minimum}. "
        f"Cite {_id_list(unused)} where they support a claim in the document.",
        f"{len(used)} cited < {minimum}",
    )


def _shorten(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
