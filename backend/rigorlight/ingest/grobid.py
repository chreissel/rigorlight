"""Parse PDFs with GROBID and turn its TEI XML into a `Paper`.

GROBID runs locally (see docker-compose.yml); URL from env GROBID_URL.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from rigorlight.ingest.doi import normalize_doi
from rigorlight.ingest.paper import Paper, Reference

DEFAULT_GROBID_URL = "http://localhost:8070"
GROBID_TIMEOUT_S = 180.0
NS = {"tei": "http://www.tei-c.org/ns/1.0"}
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.I)


class GrobidError(RuntimeError):
    pass


def grobid_url() -> str:
    return os.environ.get("GROBID_URL", DEFAULT_GROBID_URL).rstrip("/")


def process_pdf(pdf: Path | str | bytes, base_url: str | None = None,
                client: httpx.Client | None = None) -> str:
    """Send a PDF to GROBID's /api/processFulltextDocument and return the TEI XML."""
    data = pdf if isinstance(pdf, bytes) else Path(pdf).read_bytes()
    url = f"{base_url or grobid_url()}/api/processFulltextDocument"
    form = {"includeRawCitations": "1", "consolidateHeader": "0", "consolidateCitations": "0"}
    client = client or httpx.Client(timeout=GROBID_TIMEOUT_S)
    try:
        resp = client.post(url, files={"input": ("paper.pdf", data, "application/pdf")}, data=form)
    except httpx.HTTPError as e:
        raise GrobidError(f"Could not reach GROBID at {url} ({e}). "
                          "Start it with `docker compose up -d grobid` or set GROBID_URL.") from e
    if resp.status_code != 200:
        raise GrobidError(f"GROBID returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.text


def parse_pdf(pdf: Path | str | bytes, base_url: str | None = None,
              client: httpx.Client | None = None) -> Paper:
    return parse_tei(process_pdf(pdf, base_url, client))


# -- TEI parsing ---------------------------------------------------------------------
def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _year(when: str | None) -> int | None:
    m = re.match(r"(\d{4})", when or "")
    return int(m.group(1)) if m else None


def _authors(parent: ET.Element | None) -> list[str]:
    if parent is None:
        return []
    out = []
    for pers in parent.findall("tei:author/tei:persName", NS):
        surname = _text(pers.find("tei:surname", NS))
        forenames = " ".join(_text(f) for f in pers.findall("tei:forename", NS))
        if surname:
            out.append(f"{surname}, {forenames}".rstrip(", "))
    return out


def _parse_bibl(bs: ET.Element) -> Reference:
    analytic = bs.find("tei:analytic", NS)
    monogr = bs.find("tei:monogr", NS)
    a_title = _text(analytic.find("tei:title", NS)) if analytic is not None else ""
    m_title_el = monogr.find("tei:title", NS) if monogr is not None else None
    m_title = _text(m_title_el)
    m_level = m_title_el.get("level", "") if m_title_el is not None else ""

    raw = _text(bs.find("tei:note[@type='raw_reference']", NS))
    doi = next((normalize_doi(_text(i)) for i in bs.iter(f"{{{NS['tei']}}}idno")
                if i.get("type", "").lower() == "doi"), None)
    urls = [p.get("target") for p in bs.iter(f"{{{NS['tei']}}}ptr") if p.get("target")]
    urls += URL_RE.findall(raw)
    urls = list(dict.fromkeys(u.rstrip(".,;)") for u in urls))
    date = bs.find(".//tei:imprint/tei:date", NS)
    vol = bs.find(".//tei:imprint/tei:biblScope[@unit='volume']", NS)
    page = bs.find(".//tei:imprint/tei:biblScope[@unit='page']", NS)
    first_page = (page.get("from") or _text(page).split("-")[0].strip()) if page is not None else ""

    if a_title:
        title, venue, kind = a_title, m_title, "article"
    elif m_title and m_level == "m":
        title, venue, kind = m_title, "", "book"
    elif m_title:
        title, venue, kind = m_title, "", "article"
    else:
        title, venue, kind = "", "", "web" if urls else "other"

    return Reference(
        raw=raw,
        title=title,
        authors=_authors(analytic) or _authors(monogr),
        year=_year(date.get("when") if date is not None else None) or _year(_text(date)),
        doi=doi or None,
        urls=urls,
        venue=venue,
        volume=_text(vol) or None,
        first_page=first_page or None,
        kind=kind,
    )


def parse_tei(xml: str | bytes) -> Paper:
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    title = _text(root.find(".//tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:title", NS))
    abstract = _text(root.find(".//tei:teiHeader/tei:profileDesc/tei:abstract", NS))
    doi_el = root.find(".//tei:teiHeader//tei:sourceDesc//tei:idno[@type='DOI']", NS)
    refs = [_parse_bibl(bs) for bs in root.findall(".//tei:text/tei:back//tei:listBibl/tei:biblStruct", NS)]
    return Paper(
        title=title,
        abstract=abstract,
        references=refs,
        doi=normalize_doi(_text(doi_el)) if doi_el is not None else None,
        source="grobid",
    )
