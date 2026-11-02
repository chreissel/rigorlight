"""Ingest a paper by DOI.

Metadata comes from Crossref (falling back to OpenAlex, e.g. for arXiv/DataCite DOIs).
If the publisher deposited a reference list with Crossref, it is used directly.
Otherwise an open-access PDF (arXiv, OpenAlex best OA location) can be fetched and
parsed with GROBID.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from rigorlight.net import Http

from rigorlight.ingest.paper import Paper, Reference

CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"
DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)
ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
MAX_PDF_BYTES = 50 * 1024 * 1024


def normalize_doi(s: str | None) -> str | None:
    """'https://doi.org/10.1/ABC.' -> '10.1/abc'. Returns None if no DOI is present."""
    if not s:
        return None
    m = DOI_RE.search(s.strip())
    if not m:
        return None
    return m.group(0).rstrip(".,;)]}>").lower()


def looks_like_doi(s: str) -> bool:
    return normalize_doi(s) is not None


def q(doi: str) -> str:
    return quote(doi, safe="/")


def crossref_work(doi: str, http: Http) -> dict | None:
    data = http.get_json(f"{CROSSREF}/works/{q(doi)}")
    return data.get("message") if data else None


def openalex_work(doi: str, http: Http) -> dict | None:
    return http.get_json(f"{OPENALEX}/works/doi:{q(doi)}")


def crossref_title(msg: dict) -> str:
    return " ".join((msg.get("title") or [""])[0].split())


def crossref_year(msg: dict) -> int | None:
    for k in ("published-print", "published-online", "issued", "published", "created"):
        parts = (msg.get(k) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def _crossref_ref(item: dict) -> Reference:
    m = re.match(r"\d{4}", str(item.get("year", "")))
    title = item.get("article-title") or ""
    venue = item.get("journal-title") or ""
    kind = "article"
    if not title and item.get("volume-title"):
        title, kind = item["volume-title"], "book"
    raw = item.get("unstructured") or ""
    urls = list(dict.fromkeys(u.rstrip(".,;)") for u in re.findall(r"https?://[^\s<>\"]+", raw)))
    if not title and not item.get("DOI") and urls:
        kind = "web"
    return Reference(
        raw=raw,
        title=" ".join(title.split()),
        authors=[item["author"]] if item.get("author") else [],
        year=int(m.group(0)) if m else None,
        doi=normalize_doi(item.get("DOI")),
        urls=urls,
        venue=venue,
        volume=str(item["volume"]) if item.get("volume") else None,
        first_page=str(item["first-page"]) if item.get("first-page") else None,
        kind=kind,
    )


def open_access_pdf_url(doi: str, http: Http) -> str | None:
    """Best guess for a legally free PDF of the paper (arXiv or OpenAlex OA location)."""
    m = ARXIV_DOI_RE.match(doi)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}"
    work = openalex_work(doi, http) or {}
    loc = work.get("best_oa_location") or {}
    if loc.get("pdf_url"):
        return loc["pdf_url"]
    for loc in work.get("locations") or []:
        if loc.get("is_oa") and loc.get("pdf_url"):
            return loc["pdf_url"]
    return None


def download_pdf(url: str, http: Http, dest: Path | None = None) -> bytes:
    resp = http.request("GET", url)
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
        raise ValueError(f"{url} did not return a PDF (HTTP {resp.status_code}, {ctype})")
    if len(resp.content) > MAX_PDF_BYTES:
        raise ValueError(f"{url}: PDF larger than {MAX_PDF_BYTES // 2**20} MB")
    if dest:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
    return resp.content


def paper_from_doi(doi: str, http: Http, allow_pdf: bool = True,
                   grobid_url: str | None = None) -> Paper:
    """Build a Paper from a DOI. Uses Crossref's reference list if present,
    otherwise (if allowed) an open-access PDF parsed by GROBID."""
    doi = normalize_doi(doi) or doi
    paper = Paper(doi=doi, source="crossref")
    msg = crossref_work(doi, http)
    if msg:
        paper.title = crossref_title(msg)
        paper.abstract = re.sub(r"<[^>]+>", "", msg.get("abstract") or "").strip()
        paper.references = [_crossref_ref(r) for r in msg.get("reference") or []]
        if msg.get("reference-count") and not paper.references:
            paper.notes.append("Publisher did not make its reference list public on Crossref.")
    else:
        work = openalex_work(doi, http)
        if not work:
            raise ValueError(f"DOI {doi} not found on Crossref or OpenAlex")
        paper.title = work.get("display_name") or ""
        paper.source = "openalex"

    if paper.references or not allow_pdf:
        if not paper.references:
            paper.notes.append("No reference list available (PDF fetching disabled).")
        return paper

    pdf_url = open_access_pdf_url(doi, http)
    if not pdf_url:
        paper.notes.append("No reference list on Crossref and no open-access PDF found.")
        return paper
    from rigorlight.ingest import grobid  # local import: avoids a circular import
    try:
        parsed = grobid.parse_pdf(download_pdf(pdf_url, http), base_url=grobid_url)
    except Exception as e:  # noqa: BLE001 - report in the output, don't crash
        paper.notes.append(f"Could not parse open-access PDF {pdf_url}: {e}")
        return paper
    parsed.doi = doi
    parsed.title = paper.title or parsed.title
    parsed.notes = paper.notes + [f"References extracted by GROBID from {pdf_url}"]
    return parsed
