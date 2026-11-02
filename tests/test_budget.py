import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from rigorlight.budget import (
    BudgetExceeded,
    BudgetGuard,
    BudgetNotConfigured,
    cost_from_usage,
    estimate_max_cost,
)
from rigorlight.llm import LLM

PACKAGE = Path(__file__).resolve().parents[1] / "backend" / "rigorlight"


def test_refuses_without_budget(monkeypatch, tmp_path):
    monkeypatch.delenv("MONTHLY_BUDGET_USD", raising=False)
    with pytest.raises(BudgetNotConfigured):
        BudgetGuard(ledger_path=tmp_path / "l.db")


def test_refuses_zero_budget(tmp_path):
    with pytest.raises(BudgetNotConfigured):
        BudgetGuard(0, ledger_path=tmp_path / "l.db")


def test_reservations_cannot_exceed_budget(tmp_path):
    g = BudgetGuard(1.00, ledger_path=tmp_path / "l.db")
    g.reserve(0.60, "claude-opus-5-5")
    with pytest.raises(BudgetExceeded):
        g.reserve(0.50, "claude-opus-5-5")
    assert g.spent_this_month() == pytest.approx(0.60)


def test_settle_replaces_estimate_with_actual(tmp_path):
    g = BudgetGuard(1.00, ledger_path=tmp_path / "l.db")
    r = g.reserve(0.90, "claude-opus-5-5")
    g.settle(r, 0.05)
    assert g.spent_this_month() == pytest.approx(0.05)
    g.reserve(0.90, "claude-opus-5-5")  # room again


def test_estimate_is_upper_bound_of_actual():
    usage = SimpleNamespace(input_tokens=10_000, output_tokens=2_000,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    actual = cost_from_usage("claude-opus-5-5", usage)
    # 10k tokens of English is ~40k characters; estimate assumes 2 chars/token.
    assert estimate_max_cost("claude-opus-5-5", prompt_chars=40_000, max_tokens=2_000) >= actual


class FakeClient:
    def __init__(self, usage, stop_reason="end_turn"):
        resp = SimpleNamespace(
            usage=usage, stop_reason=stop_reason,
            content=[SimpleNamespace(type="text", text="ok")],
        )
        self.calls = 0

        def create(**kwargs):
            self.calls += 1
            return resp

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_llm_records_actual_cost(tmp_path):
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    g = BudgetGuard(100, ledger_path=tmp_path / "l.db")
    llm = LLM(guard=g, client=FakeClient(usage))
    assert llm.ask("sys", "hello", max_tokens=100) == "ok"
    assert g.spent_this_month() == pytest.approx(4.00)  # 1M input tokens on Opus 5.5


def test_llm_does_not_call_api_when_over_budget(tmp_path):
    g = BudgetGuard(0.01, ledger_path=tmp_path / "l.db")
    client = FakeClient(SimpleNamespace())
    llm = LLM(guard=g, client=client)
    with pytest.raises(BudgetExceeded):
        llm.ask("sys", "x" * 100_000, max_tokens=8000)
    assert client.calls == 0


def test_only_llm_module_imports_anthropic():
    """The budget cap can only be enforced if all API calls go through llm.py."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "llm.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "anthropic" or n.startswith("anthropic.") for n in names):
                offenders.append(str(path))
    assert not offenders, f"Only llm.py may import anthropic; found in: {offenders}"
