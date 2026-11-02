"""Plain data model for an ingested paper and its references."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reference:
    raw: str = ""                 # citation string as printed in the paper (if known)
    title: str = ""
    authors: list[str] = field(default_factory=list)   # "Surname, Forename" or as given
    year: int | None = None
    doi: str | None = None        # normalised, lower-case, no https://doi.org/ prefix
    urls: list[str] = field(default_factory=list)
    venue: str = ""               # journal / book / proceedings title
    volume: str | None = None
    first_page: str | None = None
    kind: str = "article"         # article | book | web | other


@dataclass
class Paper:
    title: str = ""
    abstract: str = ""
    references: list[Reference] = field(default_factory=list)
    doi: str | None = None
    source: str = ""              # "grobid" or "crossref"
    notes: list[str] = field(default_factory=list)  # ingest warnings shown in reports
