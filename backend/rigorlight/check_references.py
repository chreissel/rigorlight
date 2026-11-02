"""Command line: check a paper's references.

    python -m rigorlight.check_references paper.pdf
    python -m rigorlight.check_references 10.1038/nature14539 [--json]

Uses only free services (GROBID, Crossref, OpenAlex, doi.org, Wayback Machine;
Semantic Scholar if S2_API_KEY is set). Set CONTACT_EMAIL for the polite pools.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rigorlight.checks.references import check_references
from rigorlight.checks.result import CheckResult, Status
from rigorlight.ingest import grobid
from rigorlight.ingest.doi import looks_like_doi, paper_from_doi
from rigorlight.net import DEFAULT_CACHE_DIR, Http, LookupFailed, contact_email


def format_report(title: str, result: CheckResult) -> str:
    s = result.stats
    lines = [
        f"Paper: {title or '(untitled)'}",
        f"{Status(result.status).emoji} References: {result.status.value.upper()} - {result.summary}",
        f"   {s.get('total', 0)} references: {s.get('verified', 0)} verified, "
        f"{s.get('discrepancy', 0)} with discrepancies, "
        f"{s.get('mismatch', 0)} mismatch, {s.get('not_found', 0)} not found, "
        f"{s.get('retracted', 0)} retracted, {s.get('unverifiable', 0)} unverifiable; "
        f"URLs: {s.get('urls_dead', 0)} dead of {s.get('urls_checked', 0)} checked",
    ]
    if result.evidence:
        lines.append("")
        lines.append("Evidence:")
        for e in result.evidence:
            lines.append(f"  - {e.label}: {e.detail}")
    lines.append("")
    lines.append("Flags are for human review, not verdicts.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rigorlight.check_references",
                                 description="Check a paper's references (no LLM calls).")
    ap.add_argument("source", help="path to a PDF, or a DOI")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--no-urls", action="store_true", help="skip checking cited URLs")
    ap.add_argument("--no-pdf", action="store_true",
                    help="for DOIs: don't fetch an open-access PDF if Crossref has no reference list")
    ap.add_argument("--no-cache", action="store_true", help=f"don't use the cache in {DEFAULT_CACHE_DIR}")
    ap.add_argument("--grobid-url", help="GROBID base URL (default: $GROBID_URL or http://localhost:8070)")
    args = ap.parse_args(argv)

    if not contact_email():
        print("Note: set CONTACT_EMAIL to use the Crossref/OpenAlex polite pools.", file=sys.stderr)
    http = Http(cache_dir=None if args.no_cache else DEFAULT_CACHE_DIR)

    try:
        path = Path(args.source)
        if path.exists():
            paper = grobid.parse_pdf(path, base_url=args.grobid_url)
        elif looks_like_doi(args.source):
            paper = paper_from_doi(args.source, http, allow_pdf=not args.no_pdf,
                                   grobid_url=args.grobid_url)
        else:
            ap.error(f"{args.source!r} is neither an existing file nor a DOI")
    except (grobid.GrobidError, LookupFailed, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    def progress(i: int, n: int) -> None:
        if not args.json and sys.stderr.isatty():
            print(f"\rChecking reference {i}/{n}...", end="", file=sys.stderr, flush=True)

    result = check_references(paper, http, check_urls=not args.no_urls, progress=progress)
    if not args.json and sys.stderr.isatty():
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    if args.json:
        out = result.to_dict()
        out["paper"] = {"title": paper.title, "doi": paper.doi, "source": paper.source}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_report(paper.title, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
