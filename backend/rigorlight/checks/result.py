"""Shared result format used by every Rigorlight check.

Each check returns one `CheckResult` with a traffic-light `status`, a one-sentence
`summary` and a list of `Evidence` items that back the status up.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    GREEN = "green"    # passed
    YELLOW = "yellow"  # some flaws
    RED = "red"        # failed
    GREY = "grey"      # could not check

    @property
    def emoji(self) -> str:
        return {"green": "🟢", "yellow": "🟡", "red": "🔴", "grey": "⚪"}[self.value]


@dataclass
class Evidence:
    label: str                      # short tag, e.g. "not_found" or "dead_url"
    detail: str                     # human-readable explanation of what was found
    data: dict[str, Any] = field(default_factory=dict)  # machine-readable extras


@dataclass
class CheckResult:
    check: str                      # name of the check, e.g. "references"
    status: Status
    summary: str                    # one sentence
    evidence: list[Evidence] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kwargs)
