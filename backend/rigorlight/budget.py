"""Hard monthly spending cap for all Claude API calls.

How it works
------------
* ``MONTHLY_BUDGET_USD`` MUST be set. If it is missing, every LLM call is refused.
* Before each call we *reserve* the worst-case cost of that call
  (estimated input tokens + ``max_tokens`` of output, at the most expensive rate
  the call could be billed at). If the reservation would push this calendar
  month's total over the budget, the call is refused before anything is sent.
* After the call we *settle* the reservation to the real cost from
  ``response.usage``.
* The ledger is a small SQLite file, and reservations take a write lock, so
  several workers running in parallel cannot jointly overshoot the budget.

This is a second line of defence. The first line is the monthly spend limit you
set in the Anthropic Console (see docs/COSTS.md) - that one is enforced by
Anthropic even if this code has a bug.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# USD per 1M tokens. Source: Anthropic pricing as of 2026-10.
# Re-check https://www.anthropic.com/pricing when changing models.
# cache_write is the 5-minute-TTL write price (1.25x input).
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    # Models a refused request may be re-run on by server-side fallbacks.
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
}

# Models a request may silently fall back to (server-side ``fallbacks``).
FALLBACK_TARGETS: dict[str, list[str]] = {
    "claude-opus-5-5": ["claude-opus-5", "claude-opus-4-8"],
    "claude-sonnet-5-5": ["claude-sonnet-5"],
}

# Conservative: assume ~2 characters per token (real English text is ~4).
CHARS_PER_TOKEN_ESTIMATE = 2

DEFAULT_LEDGER = Path(os.environ.get("RIGORLIGHT_LEDGER", "data/spend_ledger.sqlite3"))


class BudgetError(RuntimeError):
    """Base class for budget refusals."""


class BudgetNotConfigured(BudgetError):
    pass


class BudgetExceeded(BudgetError):
    pass


class UnknownModel(BudgetError):
    pass


def _month_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def _price(model: str) -> dict[str, float]:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModel(
            f"No price known for model {model!r}; add it to PRICES in budget.py before using it."
        ) from None


def worst_case_rates(model: str) -> dict[str, float]:
    """Highest per-token rates this model's request could be billed at (incl. fallbacks)."""
    candidates = [model, *FALLBACK_TARGETS.get(model, [])]
    rates = [_price(m) for m in candidates]
    return {k: max(r[k] for r in rates) for k in rates[0]}


def estimate_max_cost(model: str, prompt_chars: int, max_tokens: int) -> float:
    """Upper bound on the cost of one request, in USD."""
    rates = worst_case_rates(model)
    input_tokens = prompt_chars / CHARS_PER_TOKEN_ESTIMATE
    # Treat every input token as a cache write: the most expensive input category.
    in_rate = max(rates["input"], rates["cache_write"])
    return (input_tokens * in_rate + max_tokens * rates["output"]) / 1_000_000


def cost_from_usage(model: str, usage, fallback_ran: bool = False) -> float:
    """Actual cost of a finished request from its ``usage`` object."""
    rates = worst_case_rates(model) if fallback_ran else _price(model)
    g = lambda name: getattr(usage, name, 0) or 0  # noqa: E731
    return (
        g("input_tokens") * rates["input"]
        + g("cache_creation_input_tokens") * rates["cache_write"]
        + g("cache_read_input_tokens") * rates["cache_read"]
        + g("output_tokens") * rates["output"]
    ) / 1_000_000


@dataclass
class Reservation:
    id: int
    amount: float


class BudgetGuard:
    def __init__(self, monthly_budget_usd: float | None = None, ledger_path: Path | str | None = None):
        if monthly_budget_usd is None:
            raw = os.environ.get("MONTHLY_BUDGET_USD", "").strip()
            if not raw:
                raise BudgetNotConfigured(
                    "MONTHLY_BUDGET_USD is not set. Refusing to call the Claude API without a spending cap. "
                    "Set it in your .env file (see .env.example)."
                )
            monthly_budget_usd = float(raw)
        if monthly_budget_usd <= 0:
            raise BudgetNotConfigured("MONTHLY_BUDGET_USD must be a positive number.")
        self.budget = float(monthly_budget_usd)
        self.ledger_path = Path(ledger_path or DEFAULT_LEDGER)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    month TEXT NOT NULL,
                    created REAL NOT NULL,
                    model TEXT NOT NULL,
                    purpose TEXT,
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'settled')),
                    amount_usd REAL NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.ledger_path, timeout=30, isolation_level=None)
        return db

    def spent_this_month(self) -> float:
        """Settled costs plus open reservations for the current calendar month (UTC)."""
        with self._connect() as db:
            (total,) = db.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE month = ?", (_month_key(),)
            ).fetchone()
        return float(total)

    def remaining(self) -> float:
        return self.budget - self.spent_this_month()

    def reserve(self, amount: float, model: str, purpose: str = "") -> Reservation:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")  # write lock: no other worker can reserve concurrently
            (spent,) = db.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE month = ?", (_month_key(),)
            ).fetchone()
            if spent + amount > self.budget:
                db.execute("ROLLBACK")
                raise BudgetExceeded(
                    f"Monthly budget reached: spent ${spent:.2f} of ${self.budget:.2f}; "
                    f"this request could cost up to ${amount:.2f}. Refusing."
                )
            cur = db.execute(
                "INSERT INTO spend (month, created, model, purpose, status, amount_usd) "
                "VALUES (?, ?, ?, ?, 'reserved', ?)",
                (_month_key(), time.time(), model, purpose, amount),
            )
            db.execute("COMMIT")
            return Reservation(id=cur.lastrowid, amount=amount)
        finally:
            db.close()

    def settle(self, reservation: Reservation, actual_usd: float) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE spend SET status = 'settled', amount_usd = ? WHERE id = ?",
                (actual_usd, reservation.id),
            )


if __name__ == "__main__":  # python -m rigorlight.budget
    g = BudgetGuard()
    spent = g.spent_this_month()
    print(f"{_month_key()}: ${spent:.2f} of ${g.budget:.2f} used ({100 * spent / g.budget:.1f}%), "
          f"${g.remaining():.2f} left")
