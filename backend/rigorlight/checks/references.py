"""Reference checker: does every cited reference exist, match, and still stand?

Uses only free services - Crossref, OpenAlex, doi.org, the Wayback Machine, and
Semantic Scholar if S2_API_KEY is set. No LLM calls.

Per-reference labels
--------------------
verified      found; the title matches 100% (ignoring case, punctuation, accents) and every
              other field both sides have - authors, journal, year, volume, first page - agrees
discrepancy   it is the same work, but the title is not an exact match or another field
              differs (e.g. wrong author list, journal, year or page numbers)
mismatch      the DOI resolves, but to a clearly different paper
not_found     a citable article with a usable title that no database knows about
retracted     the cited work has been retracted (Crossref / Retraction Watch, OpenAlex)
unverifiable  books, web pages, too little information, or the lookup service failed

Traffic light (thresholds below, applied in this order)
-------------------------------------------------------
red     any retracted reference, or >= RED_MIN_BAD not_found/mismatch
        (several unfindable or wrong references are a strong sign of fabricated citations)
yellow  exactly one not_found/mismatch, any reference whose author list differs from the
        published record, > YELLOW_DISCREPANCY_FRACTION of references with other
        discrepancies, or > YELLOW_DEAD_URL_FRACTION of cited URLs dead
grey    no references, or > GREY_UNVERIFIABLE_FRACTION of references unverifiable
green   otherwise
"""
from __future__ import annotations

import os
import re
import unicodedata
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
YELLOW_DISCREPANCY_FRACTION = 0.10  # > 10% of references with metadata discrepancies -> yellow
YELLOW_ANY_AUTHOR_DIFF = True      # a single reference with wrong/missing authors -> yellow
TITLE_EXACT = 100                  # title must match 100% (after normalising case, punctuation,
                                   # accents, HTML tags) to count as verified
TITLE_MISMATCH = 60                # below this the DOI points to a different paper
SEARCH_MATCH = 90                  # title search (OpenAlex/S2): below 0.9 similarity = not found;
                                   # 0.9-0.99 = found, but flagged as a title discrepancy
YEAR_TOLERANCE = 1                 # preprint vs. published year
VENUE_STOPWORDS = {"of", "the", "and", "in", "on", "for", "a", "an", "de", "der", "und"}
MIN_TITLE_WORDS = 3                # shorter titles are too vague to search for
URL_TIMEOUT_S = 8.0
URL_ALIVE_IF_BLOCKED = {401, 403, 405, 406, 429}   # server answered but blocks bots

RETRACTION_TYPES = {"retraction", "withdrawal", "removal"}
S2_MATCH = "https://api.semanticscholar.org/graph/v1/paper/search/match"
WAYBACK = "https://archive.org/wayback/available"
DOI_HANDLE = "https://doi.org/api/handles/"

VERIFIED, DISCREPANCY, MISMATCH, NOT_FOUND, RETRACTED, UNVERIFIABLE = (
    "verified", "discrepancy", "mismatch", "not_found", "retracted", "unverifiable")
LABELS = (VERIFIED, DISCREPANCY, MISMATCH, NOT_FOUND, RETRACTED, UNVERIFIABLE)


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
    fields: list[dict] = field(default_factory=list)   # per-field comparison results
    urls: list[UrlCheck] = field(default_factory=list)


@dataclass
class Found:
    """Metadata of the work a database returned, ready for field-by-field comparison."""
    source: str
    title: str = ""
    alt_titles: list[str] = field(default_factory=list)   # e.g. "title: subtitle"
    authors: list[str] = field(default_factory=list)      # full names as given by the database
    venues: list[str] = field(default_factory=list)       # full + abbreviated journal names
    year: int | None = None
    volume: str | None = None
    first_page: str | None = None
    doi: str | None = None
    retraction: str | None = None


# -- normalisation -------------------------------------------------------------------
def norm(s: str | None) -> str:
    """Lower-case, strip HTML tags, accents and punctuation, collapse whitespace."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(default_process(s).split())


def title_similarity(a: str, b: str) -> float:
    """Lenient: token_set_ratio tolerates subtitles and word order (decides 'different paper')."""
    return fuzz.token_set_ratio(norm(a), norm(b))


def strict_similarity(a: str, b: str) -> float:
    """Strict: plain ratio on normalised strings (100 = identical after normalisation)."""
    return fuzz.ratio(norm(a), norm(b))


def _surname(author: str) -> str:
    """Last name token: 'LeCun, Yann', 'Y. LeCun', 'LeCun Y.' and 'Yann LeCun' -> 'lecun'."""
    a = author.split(",")[0] if "," in author else author
    tokens = norm(a).split()
    if "," not in author:
        tokens = [t for t in tokens if len(t) > 2] or tokens   # drop initials
    return tokens[-1] if tokens else ""


def _same_author(cited: str, found: str) -> bool:
    return bool(cited) and _surname(cited) in norm(found).split()


def _usable_title(ref: Reference) -> bool:
    return len(ref.title.split()) >= MIN_TITLE_WORDS


def _year_ok(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= YEAR_TOLERANCE


def _venue_matches(cited: str, candidates: list[str]) -> bool:
    """Exact (normalised), ISO-style abbreviation ('Phys. Rev. Lett.'), or one name contained in the other."""
    c = [t for t in norm(cited).split() if t not in VENUE_STOPWORDS]
    for cand in candidates:
        f = [t for t in norm(cand).split() if t not in VENUE_STOPWORDS]
        if not c or not f:
            continue
        if c == f or (len(c) == len(f) and all(x.startswith(y) or y.startswith(x) for x, y in zip(c, f))):
            return True
        if fuzz.token_set_ratio(" ".join(c), " ".join(f)) == 100:   # e.g. "Nature" vs "Nature (London)"
            return True
    return False


def _page(p: str | None) -> str:
    return re.sub(r"\D", "", (p or "").split("-")[0].split("–")[0])


# -- field-by-field comparison --------------------------------------------------------
def compare_fields(ref: Reference, found: Found) -> list[dict]:
    """Compare every field present on both sides. status: match | differs | not_checked."""
    out = []

    def add(name, status, cited, got, note=""):
        out.append({"field": name, "status": status, "cited": cited, "found": got, "note": note})

    titles = [t for t in [found.title, *found.alt_titles] if t]
    if ref.title and titles:
        exact = norm(ref.title) in {norm(t) for t in titles}
        sim = max(strict_similarity(ref.title, t) for t in titles)
        add("title", "match" if exact and sim >= TITLE_EXACT else "differs", ref.title, found.title,
            "" if exact else f"{sim:.0f}% similar")
    elif ref.raw and titles:
        exact = any(norm(t) in norm(ref.raw) for t in titles)   # title appears verbatim in citation
        add("title", "match" if exact else "differs", ref.raw, found.title,
            "" if exact else "title not found verbatim in citation")
    else:
        add("title", "not_checked", ref.title, found.title)

    if ref.authors and found.authors:
        missing = [a for a in ref.authors if not any(_same_author(a, f) for f in found.authors)]
        first_ok = _same_author(ref.authors[0], found.authors[0])
        note = (f"not among the authors: {', '.join(missing)}" if missing else
                "" if first_ok else "first author differs")
        add("authors", "match" if not missing and first_ok else "differs",
            "; ".join(ref.authors), "; ".join(found.authors[:10]), note)
    else:
        add("authors", "not_checked", "; ".join(ref.authors), "; ".join(found.authors[:10]))

    if ref.venue and found.venues:
        add("journal", "match" if _venue_matches(ref.venue, found.venues) else "differs",
            ref.venue, found.venues[0])
    else:
        add("journal", "not_checked", ref.venue, found.venues[0] if found.venues else "")

    if ref.year and found.year:
        add("year", "match" if _year_ok(ref.year, found.year) else "differs", ref.year, found.year)
    else:
        add("year", "not_checked", ref.year, found.year)

    if ref.volume and found.volume:
        add("volume", "match" if norm(ref.volume) == norm(found.volume) else "differs",
            ref.volume, found.volume)
    if _page(ref.first_page) and _page(found.first_page):
        add("first_page", "match" if _page(ref.first_page) == _page(found.first_page) else "differs",
            ref.first_page, found.first_page)
    return out


def _describe_diff(fields: list[dict]) -> str:
    parts = []
    for f in fields:
        if f["status"] == "differs":
            note = f" ({f['note']})" if f["note"] else ""
            parts.append(f"{f['field']}: cited \"{str(f['cited'])[:80]}\", found \"{str(f['found'])[:80]}\"{note}")
    return "; ".join(parts)


def classify(ref: Reference, found: Found, by_doi: bool) -> tuple[str, str, dict]:
    """Turn a field comparison into a label.

    verified     title matches exactly (after normalisation) and no compared field differs
    mismatch     (DOI only) title clearly different, or both authors and year differ
    discrepancy  same work, but title not exact or some field (authors, journal, ...) differs
    """
    fields = compare_fields(ref, found)
    st = {f["field"]: f["status"] for f in fields}
    sim = None
    titles = [t for t in [found.title, *found.alt_titles] if t]
    if ref.title and titles:
        sim = max(title_similarity(ref.title, t) for t in titles)
    elif ref.raw and titles:
        sim = max(fuzz.partial_ratio(norm(t), norm(ref.raw)) for t in titles)
    info = {"found_title": found.title, "found_year": found.year, "found_doi": found.doi,
            "similarity": sim, "source": found.source, "fields": fields}
    if found.retraction:
        return RETRACTED, f"Retracted (notice: {found.retraction})", info

    contradicted = st.get("authors") == "differs" and st.get("year") == "differs"
    if by_doi and ((sim is not None and sim < TITLE_MISMATCH) or contradicted):
        why = (f"\"{found.title}\" ({sim:.0f}% title similarity)" if sim is not None and sim < TITLE_MISMATCH
               else "a work with different authors and year")
        return MISMATCH, f"DOI points to a different paper: {why}; " + _describe_diff(fields), info

    diff = _describe_diff(fields)
    if diff:
        return DISCREPANCY, f"Found on {found.source}, but details differ - {diff}", info
    checked = [f["field"] for f in fields if f["status"] == "match"]
    if not checked:
        return VERIFIED, f"DOI resolves ({found.source}); no other details to compare", info
    return VERIFIED, f"Found on {found.source}; matches: {', '.join(checked)}", info


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


# -- database records -> Found ---------------------------------------------------------
def found_from_crossref(doi: str, msg: dict, http: Http) -> Found:
    title = crossref_title(msg)
    subtitle = " ".join((msg.get("subtitle") or [""])[0].split())
    return Found(
        source="crossref", title=title,
        alt_titles=[f"{title}: {subtitle}"] if subtitle else [],
        authors=[" ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "")
                 for a in msg.get("author") or []],
        venues=[*(msg.get("container-title") or []), *(msg.get("short-container-title") or [])],
        year=crossref_year(msg), volume=msg.get("volume"),
        first_page=(msg.get("page") or "").split("-")[0] or None,
        doi=doi, retraction=crossref_retraction(doi, msg, http))


def found_from_openalex(work: dict) -> Found:
    biblio = work.get("biblio") or {}
    venues = []
    for loc in [work.get("primary_location") or {}, *(work.get("locations") or [])]:
        name = (loc.get("source") or {}).get("display_name")
        if name and name not in venues:
            venues.append(name)
    return Found(
        source="openalex", title=work.get("display_name") or work.get("title") or "",
        authors=[(a.get("author") or {}).get("display_name") or a.get("raw_author_name") or ""
                 for a in work.get("authorships") or []],
        venues=venues, year=work.get("publication_year"),
        volume=biblio.get("volume"), first_page=biblio.get("first_page"),
        doi=(work.get("doi") or "").replace("https://doi.org/", "").lower() or None,
        retraction="OpenAlex: is_retracted" if work.get("is_retracted") else None)


def found_from_s2(p: dict) -> Found:
    journal = p.get("journal") or {}
    return Found(
        source="semantic_scholar", title=p.get("title") or "",
        authors=[a.get("name", "") for a in p.get("authors") or []],
        venues=[v for v in [journal.get("name"), p.get("venue")] if v],
        year=p.get("year"), volume=journal.get("volume"),
        first_page=(journal.get("pages") or "").split("-")[0].strip() or None,
        doi=((p.get("externalIds") or {}).get("DOI") or "").lower() or None,
        retraction="Semantic Scholar: isRetracted" if p.get("isRetracted") else None)


# -- per-reference checks ------------------------------------------------------------
def check_with_doi(ref: Reference, http: Http) -> tuple[str, str, dict]:
    doi = ref.doi
    msg = crossref_work(doi, http)
    if msg:
        found = found_from_crossref(doi, msg, http)
    else:
        work = http.get_json(f"{OPENALEX}/works/doi:{q(doi)}")  # e.g. arXiv / DataCite DOIs
        if not work:
            handle = http.get_json(DOI_HANDLE + q(doi), polite=False)
            if handle and handle.get("responseCode") == 1:
                return UNVERIFIABLE, "DOI is registered but no metadata is available to compare", {}
            return NOT_FOUND, f"DOI {doi} does not exist (not registered at doi.org)", {}
        found = found_from_openalex(work)
        found.doi = found.doi or doi
    return classify(ref, found, by_doi=True)


def _search_openalex(title: str, http: Http) -> list[tuple[float, Found]]:
    clean = re.sub(r"[,:|]", " ", title)
    data = http.get_json(f"{OPENALEX}/works", params={"search": clean, "per_page": 5})
    found = [found_from_openalex(w) for w in (data or {}).get("results") or []]
    return sorted(((strict_similarity(title, f.title), f) for f in found), key=lambda x: -x[0])


def _search_s2(title: str, http: Http) -> list[tuple[float, Found]]:
    key = os.environ.get("S2_API_KEY", "").strip()
    if not key:
        return []
    data = http.get_json(S2_MATCH, params={"query": title,
                                           "fields": "title,year,venue,journal,authors,externalIds,isRetracted"},
                         headers={"x-api-key": key}, polite=False)
    found = [found_from_s2(p) for p in (data or {}).get("data") or []]
    return [(strict_similarity(title, f.title), f) for f in found]


def _best_candidate(ref: Reference, candidates: list[tuple[float, Found]]):
    """Among works whose title is >= SEARCH_MATCH similar, prefer verified > retracted > discrepancy."""
    rank = {VERIFIED: 0, RETRACTED: 1, DISCREPANCY: 2}
    best = None
    for sim, f in candidates:
        if sim < SEARCH_MATCH:
            continue
        label, reason, info = classify(ref, f, by_doi=False)
        if _contradicted(info):
            continue
        if best is None or rank[label] < rank[best[0]]:
            best = (label, reason, info)
    return best


def _contradicted(info: dict) -> bool:
    st = {f["field"]: f["status"] for f in info.get("fields", [])}
    return st.get("authors") == "differs" and st.get("year") == "differs"


def check_without_doi(ref: Reference, http: Http) -> tuple[str, str, dict]:
    if ref.kind == "web":
        return UNVERIFIABLE, "Web resource (URL checked separately)", {}
    if not _usable_title(ref):
        return UNVERIFIABLE, "Too little information to look up (no usable title)", {}

    oa = _search_openalex(ref.title, http)
    best = _best_candidate(ref, oa)
    if best and best[0] != VERIFIED:
        s2_best = _best_candidate(ref, _search_s2(ref.title, http))
        if s2_best and s2_best[0] == VERIFIED:
            best = s2_best
    elif not best:
        best = _best_candidate(ref, _search_s2(ref.title, http))
    if best:
        return best

    if ref.kind == "book":
        return UNVERIFIABLE, "Book not found in scholarly databases (books are poorly covered)", {}
    s2 = " or Semantic Scholar" if os.environ.get("S2_API_KEY", "").strip() else ""
    if oa and oa[0][0] >= SEARCH_MATCH:
        f = oa[0][1]
        return NOT_FOUND, (f"Only a work with the same title but different authors and year exists: "
                           f"{'; '.join(f.authors[:3])} ({f.year})"), {"similarity": oa[0][0]}
    closest = f"; closest: \"{oa[0][1].title}\" ({oa[0][0]:.0f}%)" if oa else ""
    return NOT_FOUND, f"No matching work on OpenAlex{s2}" + closest, \
        {"similarity": oa[0][0] if oa else None}


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


def _field_differs(r: RefCheck, name: str) -> bool:
    return any(f["field"] == name and f["status"] == "differs" for f in r.fields)


def verdict(results: list[RefCheck], url_checks: list[UrlCheck]) -> tuple[Status, str]:
    n = len(results)
    if n == 0:
        return Status.GREY, "No references could be extracted, so they could not be checked."
    count = {lab: sum(r.label == lab for r in results)
             for lab in LABELS}
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
    disc_frac = count[DISCREPANCY] / n
    author_diff = [r for r in results if r.label == DISCREPANCY and _field_differs(r, "authors")]
    if (bad == 1 or dead_frac > YELLOW_DEAD_URL_FRACTION or disc_frac > YELLOW_DISCREPANCY_FRACTION
            or (YELLOW_ANY_AUTHOR_DIFF and author_diff)):
        parts = []
        if bad:
            parts.append("1 reference could not be found or does not match its DOI")
        if YELLOW_ANY_AUTHOR_DIFF and author_diff:
            k = len(author_diff)
            parts.append(f"{k} reference{'s' if k > 1 else ''} cite{'' if k > 1 else 's'} "
                         "authors that differ from the published record")
        if disc_frac > YELLOW_DISCREPANCY_FRACTION:
            k = count[DISCREPANCY]
            parts.append(f"{k} reference{'s have' if k > 1 else ' has'} details (title, authors, "
                         "journal, year or pages) that differ from the published record")
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
        if r.label in (RETRACTED, MISMATCH, NOT_FOUND, DISCREPANCY):
            evidence.append(Evidence(
                label=r.label, detail=f"[{r.index}] {_describe(r.reference)} - {r.reason}",
                data={"index": r.index, "raw": r.reference.raw, "found_title": r.found_title,
                      "found_doi": r.found_doi, "similarity": r.similarity, "source": r.source,
                      "fields": [f for f in r.fields if f["status"] == "differs"]}))
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
             for lab in LABELS}
    stats.update(total=len(results), urls_checked=len(url_checks),
                 urls_dead=sum(not u.alive for u in url_checks))
    stats["references"] = [
        {"index": r.index, "label": r.label, "reason": r.reason, "title": r.reference.title,
         "doi": r.reference.doi, "found_title": r.found_title, "found_doi": r.found_doi,
         "similarity": r.similarity, "source": r.source, "fields": r.fields}
        for r in results]
    return CheckResult(check="references", status=status, summary=summary,
                       evidence=evidence, stats=stats)
