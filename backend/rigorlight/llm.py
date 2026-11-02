"""The ONLY module allowed to call the Claude API.

Every request goes through ``BudgetGuard``: if the monthly cap would be exceeded,
nothing is sent. A test (tests/test_budget.py) fails if any other module imports
the ``anthropic`` package, so the cap cannot be bypassed by accident.
"""

from __future__ import annotations

import os

import anthropic

from .budget import BudgetGuard, cost_from_usage, estimate_max_cost

DEFAULT_MODEL = os.environ.get("RIGORLIGHT_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("RIGORLIGHT_EFFORT", "medium")

# Models that support server-side refusal fallbacks (`fallbacks: "default"`).
_SUPPORTS_FALLBACKS = {"claude-opus-5-5", "claude-sonnet-5-5"}


class RefusedError(RuntimeError):
    pass


class LLM:
    def __init__(self, guard: BudgetGuard | None = None, client=None, model: str = DEFAULT_MODEL):
        self.guard = guard or BudgetGuard()  # raises if MONTHLY_BUDGET_USD is not set
        self.client = client or anthropic.Anthropic()
        self.model = model

    def ask(self, system: str, user: str, *, max_tokens: int = 8000, purpose: str = "",
            effort: str = DEFAULT_EFFORT) -> str:
        """Send one request and return the text answer. Refuses if over budget."""
        reservation = self.guard.reserve(
            estimate_max_cost(self.model, len(system) + len(user), max_tokens),
            model=self.model,
            purpose=purpose,
        )
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort},
        )
        if self.model in _SUPPORTS_FALLBACKS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            response = self.client.beta.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            if 400 <= e.status_code < 500 and e.status_code not in (408, 409, 429):
                self.guard.settle(reservation, 0.0)  # rejected up front: not billed
            # Otherwise (timeouts, 5xx, 429 after retries) keep the reservation: we
            # can't be sure it wasn't billed, so we stay on the safe side.
            raise

        fallback_ran = any(
            getattr(it, "type", None) == "fallback_message"
            for it in (getattr(response.usage, "iterations", None) or [])
        )
        self.guard.settle(reservation, cost_from_usage(self.model, response.usage, fallback_ran))

        if response.stop_reason == "refusal":
            raise RefusedError(f"Model declined the request: {getattr(response, 'stop_details', None)}")
        return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
