"""Reference checker: does every cited reference exist, match, and still stand?

Uses only free services - Crossref, OpenAlex, doi.org, the Wayback Machine, and
Semantic Scholar if S2_API_KEY is set. No LLM calls.

Per-reference labels
--------------------
verified      found, and the title (or author/year) matches what the paper cites
mismatch      the DOI resolves, but to a clearly different paper
not_found     a citable article with a usable title that no database knows about
retracted     the cited work has been retracted (Crossref / Retraction Watch, OpenAlex)
unverifiable  books, web pages, too little information, or the lookup service failed

Traffic light (thresholds below, applied in this order)
-------------------------------------------------------
red     any retracted reference, or >= RED_MIN_BAD not_found/mismatch
        (several unfindable or wrong references are a strong sign of fabricated citations)
yellow  exactly one not_found/mismatch, or > YELLOW_DEAD_URL_FRACTION of cited URLs dead
grey    no references, or > GREY_UNVERIFIABLE_FRACTION of references unverifiable
green   otherwise
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
import httpx
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from rigorlight.checks.result import CheckResult, Evidence, Status
from rigorlight.ingest.doi import (CROSSREF, OPENALEX, crossref_title, crossref_work,
                                   crossref_year, q)
from rigorlight.ingest.paper import Paper, Reference
from rigorlight.net import Http, LookupFailed

# -- thresholds -------------------------------------------------------------------
RED_MIN_BAD = 2                    # >= this many not_found + mismatch -> red
YELLOW_DEAD_URL_FRACTION = 0.10    # > 10% of checked URLs dead -> yellow
GREY_UNVERIFIABLE_FRACTION = 0.50  # > 50% of references unverifiable -> grey
TITLE_MATCH = 90                   # DOI title similarity (0-100) that counts as a match
TITLE_MISMATCH = 60                # below this the DOI points to a different paper
SEARCH_MATCH = 90                  # title search (OpenAlex/S2): accept only >= 0.9 similarity
YEAR_TOLERANCE = 1                 # preprint vs. published year
MIN_TITLE_WORDS = 3                # shorter titles are too vague to search for
URL_TIMEOUT_S = 8.0
URL_ALIVE_IF_BLOCKED = {401, 403, 405, 406, 429}   # server answered but blocks bots

RETRACTION_TYPES = {"retraction", "withdrawal", "removal"}
S2_MATCH = "https://api.semanticscholar.org/graph/v1/paper/search/match"
WAYBACK = "https://archive.org/wayback/available"
DOI_HANDLE = "https://doi.org/api/handles/"

VERIFIED, MISMATCH, NOT_FOUND, RETRACTED, UNVERIFIABLE = (
    "verified", "mismatch", "not_found", "retracted", "unverifiable")


@dataclass
class UrlCheck:
    url: str
    alive: bool
    status: int | None = None
    error: str = ""
    archived: str | None = None    # Wayback snapshot for dead URLs


@dataclass
class RefCheck:
    index: int
    label: str
    reason: str
    reference: Reference
    found_title: str = ""
    found_year: int | None = None
    found_doi: str | None = None
    similarity: float | None = None
    source: str = ""
    urls: list[UrlCheck] = field(default_factory=list)


# -- helpers -----------------------------------------------------------------------
def title_similarity(a: str, b: str) -> float:
    """Lenient: token_set_ratio tolerates subtitles and word order (for DOI checks)."""
    return fuzz.token_set_ratio(a, b, processor=default_process)


def strict_similarity(a: str, b: str) -> float:
    """Strict: plain ratio, so a short query can't match a longer different title."""
    return fuzz.ratio(a, b, processor=default_process)


def _surname(author: str) -> str:
    a = author.strip()
    if "," in a:
        a = a.split(",")[0]
    else:
        a = a.split()[-1] if a.split() else ""
    return default_process(a)


def _usable_title(ref: Reference) -> bool:
    return len(ref.title.split()) >= MIN_TITLE_WORDS


def _year_ok(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= YEAR_TOLERANCE


# -- retraction info ---------------------------------------------------------------
def crossref_retraction(doi: str, msg: dict, http: Http) -> str | None:
    """Return a retraction notice DOI/description if `doi` was retracted, else None."""
    for upd in msg.get("updated-by") or []:
        if str(upd.get("type", "")).lower() in RETRACTION_TYPES:
            return upd.get("DOI") or upd.get("type")
    if (msg.get("title") or [""])[0].lower().startswith(("retracted", "withdrawn")):
        return "title marked as retracted"
    data = http.get_json(f"{CROSSREF}/works", params={"filter": f"updates:{doi}", "rows": 5})
    for item in ((data or {}).get("message") or {}).get("items") or []:
        for upd in item.get("update-to") or []:
            if (str(upd.get("type", "")).lower() in RETRACTION_TYPES
                    and (upd.get("DOI") or "").lower() == doi):
                return item.get("DOI") or upd.get("type")
    return None


# -- per-reference checks ------------------------------------------------------------
def check_with_doi(ref: Reference, http: Http) -> tuple[str, str, dict]:
    doi = ref.doi
    msg = crossref_work(doi, http)
    if msg:
        found_title, found_year, source = crossref_title(msg), crossref_year(msg), "crossref"
        found_authors = [a.get("family", "") for a in msg.get("author") or []]
        retraction = crossref_retraction(doi, msg, http)
    else:
        work = http.get_json(f"{OPENALEX}/works/doi:{q(doi)}")  # e.g. arXiv / DataCite DOIs
        if not work:
            handle = http.get_json(DOI_HANDLE + q(doi), polite=False)
            if handle and handle.get("responseCode") == 1:
                return UNVERIFIABLE, "DOI is registered but no metadata is available to compare", {}
            return NOT_FOUND, f"DOI {doi} does not exist (not registered at doi.org)", {}
        found_title, found_year, source = work.get("display_name") or "", work.get("publication_year"), "openalex"
        found_authors = [((a.get("author") or {}).get("display_name") or "")
                         for a in work.get("authorships") or []]
        retraction = "OpenAlex: is_retracted" if work.get("is_retracted") else None

    found = {"found_title": found_title, "found_year": found_year, "found_doi": doi, "source": source}
    if retraction:
        return RETRACTED, f"Retracted (notice: {retraction})", found

    author_ok = None
    if ref.authors and found_authors:
        found_surnames = {_surname(a) for a in found_authors if a}
        author_ok = _surname(ref.authors[0]) in found_surnames

    cited = ref.title or ref.raw
    if not cited or not found_title:
        if not _year_ok(ref.year, found_year) and author_ok is False:
            return MISMATCH, "DOI resolves to a work with different authors and year", found
        return VERIFIED, "DOI resolves (no title to compare)", found

    sim = title_similarity(ref.title, found_title) if ref.title else \
        fuzz.partial_ratio(default_process(found_title), default_process(ref.raw))
    found["similarity"] = sim
    year_note = "" if _year_ok(ref.year, found_year) else f" (year differs: cited {ref.year}, found {found_year})"
    if sim >= TITLE_MATCH:
        return VERIFIED, "Title matches DOI record" + year_note, found
    contradicted = author_ok is False and not _year_ok(ref.year, found_year)
    if sim >= TITLE_MISMATCH and not contradicted:
        return VERIFIED, f"Title roughly matches DOI record ({sim:.0f}%)" + year_note, found
    return MISMATCH, f"DOI points to a different paper: \"{found_title}\" ({sim:.0f}% title similarity)", found


def _search_openalex(title: str, http: Http) -> tuple[float, dict | None]:
    clean = re.sub(r"[,:|]", " ", title)
    data = http.get_json(f"{OPENALEX}/works", params={"search": clean, "per_page": 5})
    best, best_work = 0.0, None
    for w in (data or {}).get("results") or []:
        sim = strict_similarity(title, w.get("display_name") or "")
        if sim > best:
            best, best_work = sim, w
    return best, best_work


def _search_s2(title: str, http: Http) -> tuple[float, dict | None]:
    key = os.environ.get("S2_API_KEY", "").strip()
    if not key:
        return 0.0, None
    data = http.get_json(S2_MATCH, params={"query": title, "fields": "title,year,externalIds,isRetracted"},
                         headers={"x-api-key": key}, polite=False)
    for p in (data or {}).get("data") or []:
        return strict_similarity(title, p.get("title") or ""), p
    return 0.0, None


def check_without_doi(ref: Reference, http: Http) -> tuple[str, str, dict]:
    if ref.kind == "web":
        return UNVERIFIABLE, "Web resource (URL checked separately)", {}
    if not _usable_title(ref):
        return UNVERIFIABLE, "Too little information to look up (no usable title)", {}

    sim, work = _search_openalex(ref.title, http)
    if work and sim >= SEARCH_MATCH:
        found = {"found_title": work.get("display_name"), "found_year": work.get("publication_year"),
                 "found_doi": (work.get("doi") or "").replace("https://doi.org/", "") or None,
                 "similarity": sim, "source": "openalex"}
        if work.get("is_retracted"):
            return RETRACTED, "Retracted (OpenAlex: is_retracted)", found
        note = "" if _year_ok(ref.year, found["found_year"]) else \
            f" (year differs: cited {ref.year}, found {found['found_year']})"
        return VERIFIED, "Found on OpenAlex by title" + note, found

    s2_sim, paper = _search_s2(ref.title, http)
    if paper and s2_sim >= SEARCH_MATCH:
        found = {"found_title": paper.get("title"), "found_year": paper.get("year"),
                 "found_doi": ((paper.get("externalIds") or {}).get("DOI") or "").lower() or None,
                 "similarity": s2_sim, "source": "semantic_scholar"}
        if paper.get("isRetracted"):
            return RETRACTED, "Retracted (Semantic Scholar)", found
        return VERIFIED, "Found on Semantic Scholar by title", found

    if ref.kind == "book":
        return UNVERIFIABLE, "Book not found in scholarly databases (books are poorly covered)", {}
    closest = f"; closest: \"{work.get('display_name')}\" ({sim:.0f}%)" if work else ""
    s2 = " or Semantic Scholar" if os.environ.get("S2_API_KEY", "").strip() else ""
    return NOT_FOUND, f"No matching work on OpenAlex{s2}" + closest, \
        {"similarity": sim or None}


def check_reference(index: int, ref: Reference, http: Http) -> RefCheck:
    try:
        label, reason, found = check_with_doi(ref, http) if ref.doi else check_without_doi(ref, http)
    except LookupFailed as e:
        label, reason, found = UNVERIFIABLE, f"Lookup failed: {e}", {}
    return RefCheck(index=index, label=label, reason=reason, reference=ref, **found)


# -- URLs --------------------------------------------------------------------------
def _probe(url: str, http: Http) -> tuple[int | None, str]:
    status, err = None, ""
    for method in ("HEAD", "GET"):
        try:
            with http.client.stream(method, url, timeout=URL_TIMEOUT_S, follow_redirects=True) as r:
                status = r.status_code
        except httpx.HTTPError as e:
            err = type(e).__name__
            continue
        if status < 400 or status in URL_ALIVE_IF_BLOCKED and method == "GET":
            break
    return status, err


def check_url(url: str, http: Http) -> UrlCheck:
    def compute() -> dict:
        status, err = _probe(url, http)
        alive = status is not None and (status < 400 or status in URL_ALIVE_IF_BLOCKED)
        archived = None
        if not alive:
            try:
                data = http.get_json(WAYBACK, params={"url": url}, polite=False)
                snap = ((data or {}).get("archived_snapshots") or {}).get("closest") or {}
                archived = snap.get("url") if snap.get("available") else None
            except LookupFailed:
                pass
        return asdict(UrlCheck(url=url, alive=alive, status=status, error=err, archived=archived))
    return UrlCheck(**http.cached(f"URLCHECK {url}", compute))


def _is_doi_url(url: str) -> bool:
    return bool(re.match(r"https?://(dx\.)?doi\.org/", url, re.I))


# -- the check ---------------------------------------------------------------------
def _describe(ref: Reference) -> str:
    who = ref.authors[0].split(",")[0] if ref.authors else ""
    s = f"{who} ({ref.year})" if who or ref.year else ""
    title = ref.title or ref.raw or (ref.urls[0] if ref.urls else "(no details)")
    return f"{s} \"{title[:120]}\"".strip() + (f" doi:{ref.doi}" if ref.doi else "")


def verdict(results: list[RefCheck], url_checks: list[UrlCheck]) -> tuple[Status, str]:
    n = len(results)
    if n == 0:
        return Status.GREY, "No references could be extracted, so they could not be checked."
    count = {lab: sum(r.label == lab for r in results)
             for lab in (VERIFIED, MISMATCH, NOT_FOUND, RETRACTED, UNVERIFIABLE)}
    bad = count[NOT_FOUND] + count[MISMATCH]
    dead = sum(not u.alive for u in url_checks)
    dead_frac = dead / len(url_checks) if url_checks else 0.0

    if count[RETRACTED] or bad >= RED_MIN_BAD:
        parts = []
        if count[RETRACTED]:
            parts.append(f"{count[RETRACTED]} retracted")
        if bad:
            parts.append(f"{bad} not found or mismatched")
        return Status.RED, f"{' and '.join(parts)} out of {n} references."
    if bad == 1 or dead_frac > YELLOW_DEAD_URL_FRACTION:
        parts = []
        if bad:
            parts.append("1 reference could not be found or does not match its DOI")
        if dead_frac > YELLOW_DEAD_URL_FRACTION:
            parts.append(f"{dead} of {len(url_checks)} cited URLs are dead")
        return Status.YELLOW, f"{' and '.join(parts)} ({count[VERIFIED]} of {n} verified)."
    if count[UNVERIFIABLE] / n > GREY_UNVERIFIABLE_FRACTION:
        return Status.GREY, f"Only {count[VERIFIED]} of {n} references could be verified; " \
                            f"{count[UNVERIFIABLE]} are books, web pages or lack details."
    return Status.GREEN, f"{count[VERIFIED]} of {n} references verified; no problems found."


def check_references(paper: Paper, http: Http | None = None, check_urls: bool = True,
                     progress=None) -> CheckResult:
    http = http or Http()
    results = []
    for i, ref in enumerate(paper.references, 1):
        if progress:
            progress(i, len(paper.references))
        results.append(check_reference(i, ref, http))

    url_checks: list[UrlCheck] = []
    if check_urls:
        seen: dict[str, UrlCheck] = {}
        for r in results:
            for url in r.reference.urls:
                if _is_doi_url(url):
                    continue
                if url not in seen:
                    seen[url] = check_url(url, http)
                r.urls.append(seen[url])
        url_checks = list(seen.values())

    status, summary = verdict(results, url_checks)
    evidence = []
    for r in results:
        if r.label in (RETRACTED, MISMATCH, NOT_FOUND):
            evidence.append(Evidence(
                label=r.label, detail=f"[{r.index}] {_describe(r.reference)} - {r.reason}",
                data={"index": r.index, "raw": r.reference.raw, "found_title": r.found_title,
                      "found_doi": r.found_doi, "similarity": r.similarity, "source": r.source}))
    for r in results:
        for u in r.urls:
            if not u.alive:
                arch = f"; archived copy: {u.archived}" if u.archived else "; no archived copy"
                evidence.append(Evidence(
                    label="dead_url",
                    detail=f"[{r.index}] {u.url} ({u.status or u.error}){arch}",
                    data={"index": r.index, **asdict(u)}))
    if status == Status.GREY or any(r.reason.startswith("Lookup failed") for r in results):
        for r in results:
            if r.label == UNVERIFIABLE:
                evidence.append(Evidence(label=UNVERIFIABLE,
                                         detail=f"[{r.index}] {_describe(r.reference)} - {r.reason}",
                                         data={"index": r.index}))
    for note in paper.notes:
        evidence.append(Evidence(label="note", detail=note))

    stats = {lab: sum(r.label == lab for r in results)
             for lab in (VERIFIED, MISMATCH, NOT_FOUND, RETRACTED, UNVERIFIABLE)}
    stats.update(total=len(results), urls_checked=len(url_checks),
                 urls_dead=sum(not u.alive for u in url_checks))
    stats["references"] = [
        {"index": r.index, "label": r.label, "reason": r.reason, "title": r.reference.title,
         "doi": r.reference.doi, "found_title": r.found_title, "found_doi": r.found_doi,
         "similarity": r.similarity, "source": r.source}
        for r in results]
    return CheckResult(check="references", status=status, summary=summary,
                       evidence=evidence, stats=stats)
