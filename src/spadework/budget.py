"""Cost estimates per session kind. See docs/spec/04-engine.md, Budgets and limits."""

from __future__ import annotations

from typing import Literal

SessionKind = Literal["research", "draft", "repair", "critic", "fix", "summary"]

# Used when a task or its playbook does not set budget_usd.
DEFAULT_ESTIMATES_USD: dict[SessionKind, float] = {
    "research": 0.75,
    "draft": 1.00,
    "repair": 0.40,
    "critic": 1.00,
    "fix": 0.50,
    "summary": 0.50,
}
