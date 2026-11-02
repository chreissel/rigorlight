"""Reference checker tests. All HTTP is mocked with respx - no network, no GROBID."""
import json
from pathlib import Path

import httpx
import pytest
import respx

from rigorlight.checks import references as R
from rigorlight.checks.references import check_references, verdict
from rigorlight.checks.result import CheckResult, Evidence, Status
from rigorlight.ingest import grobid
from rigorlight.ingest.doi import normalize_doi, paper_from_doi
from rigorlight.ingest.paper import Paper, Reference
from rigorlight.net import Http, LookupFailed, user_agent

FIXTURE = Path(__file__).parent / "fixtures" / "sample.tei.xml"
CR = "https://api.crossref.org"
OA = "https://api.openalex.org"


@pytest.fixture
def http(tmp_path):
    return Http(cache_dir=tmp_path / "cache", client=httpx.Client(), sleep=lambda s: None, min_interval=0)


def crossref_msg(title, year, family, **extra):
    families = family if isinstance(family, list) else [family]
    return {"message": {"title": [title], "issued": {"date-parts": [[year]]},
                        "author": [{"family": f} for f in families], **extra}}


DEEP_LEARNING = crossref_msg("Deep learning", 2015, ["LeCun", "Bengio", "Hinton"],
                             **{"container-title": ["Nature"], "volume": "521", "page": "436-444"})


NO_UPDATES = {"message": {"items": []}}


# -- result format ---------------------------------------------------------------------
def test_result_is_json_serialisable():
    r = CheckResult("references", Status.YELLOW, "One problem.", [Evidence("not_found", "x", {"i": 1})])
    d = json.loads(r.to_json())
    assert d["status"] == "yellow" and d["evidence"][0]["data"] == {"i": 1}


# -- ingest ----------------------------------------------------------------------------
def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1038/NATURE14539.") == "10.1038/nature14539"
    assert normalize_doi("doi:10.48550/arXiv.1706.03762") == "10.48550/arxiv.1706.03762"
    assert normalize_doi("not a doi") is None


def test_parse_tei_fixture():
    paper = grobid.parse_tei(FIXTURE.read_text())
    assert paper.title == "A Small Test Paper About Neural Networks"
    assert paper.abstract == "We study neural networks. This is a fixture."
    assert paper.doi == "10.9999/test.2024.001"
    refs = paper.references
    assert len(refs) == 5
    assert refs[0].title == "Deep learning" and refs[0].year == 2015
    assert refs[0].doi == "10.1038/nature14539" and refs[0].authors[0] == "LeCun, Yann"
    assert refs[0].venue == "Nature" and refs[0].kind == "article"
    assert "Deep learning. Nature" in refs[0].raw
    assert refs[2].doi is None and refs[2].kind == "article"
    assert refs[3].kind == "book" and refs[3].title.startswith("Pattern Recognition")
    assert refs[4].kind == "web" and refs[4].urls == ["https://example.org/dataset"]


@respx.mock
def test_grobid_request_and_error():
    respx.post("http://grobid:8070/api/processFulltextDocument").mock(
        return_value=httpx.Response(200, text=FIXTURE.read_text()))
    paper = grobid.parse_pdf(b"%PDF-1.4 fake", base_url="http://grobid:8070")
    assert len(paper.references) == 5
    respx.post("http://down:8070/api/processFulltextDocument").mock(side_effect=httpx.ConnectError("no"))
    with pytest.raises(grobid.GrobidError, match="docker compose"):
        grobid.parse_pdf(b"%PDF", base_url="http://down:8070")


@respx.mock
def test_paper_from_doi_uses_crossref_references(http):
    respx.get(f"{CR}/works/10.1000/abc").mock(return_value=httpx.Response(200, json={"message": {
        "title": ["Host paper"], "reference-count": 2, "reference": [
            {"key": "r1", "DOI": "10.1038/NATURE14539", "article-title": "Deep learning", "year": "2015",
             "author": "LeCun"},
            {"key": "r2", "unstructured": "Bishop. Pattern recognition. Springer 2006", "volume-title":
             "Pattern Recognition and Machine Learning", "year": "2006"},
        ]}}))
    paper = paper_from_doi("https://doi.org/10.1000/ABC", http)
    assert paper.title == "Host paper" and paper.source == "crossref"
    assert paper.references[0].doi == "10.1038/nature14539"
    assert paper.references[1].kind == "book"


@respx.mock
def test_paper_from_doi_falls_back_to_arxiv_pdf(http, monkeypatch):
    doi = "10.48550/arxiv.1706.03762"
    respx.get(f"{CR}/works/{doi}").mock(return_value=httpx.Response(404))
    respx.get(f"{OA}/works/doi:{doi}").mock(return_value=httpx.Response(
        200, json={"display_name": "Attention Is All You Need"}))
    respx.get("https://arxiv.org/pdf/1706.03762").mock(return_value=httpx.Response(200, content=b"%PDF-1.5 x"))
    monkeypatch.setattr(grobid, "parse_pdf", lambda pdf, base_url=None: grobid.parse_tei(FIXTURE.read_text()))
    paper = paper_from_doi(doi, http)
    assert paper.title == "Attention Is All You Need"
    assert len(paper.references) == 5 and paper.doi == doi
    assert any("arxiv.org/pdf" in n for n in paper.notes)


# -- net -------------------------------------------------------------------------------
def test_user_agent_has_contact_email(monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", "me@example.org")
    assert "mailto:me@example.org" in user_agent()


@respx.mock
def test_cache_retry_and_mailto(http, monkeypatch, tmp_path):
    monkeypatch.setenv("CONTACT_EMAIL", "me@example.org")
    route = respx.get(f"{CR}/works/10.1/x").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    assert http.get_json(f"{CR}/works/10.1/x") == {"ok": 1}
    assert route.call_count == 2
    assert route.calls[-1].request.url.params["mailto"] == "me@example.org"
    # second call (even from a fresh client) is served from the on-disk cache
    fresh = Http(cache_dir=tmp_path / "cache", client=httpx.Client(), sleep=lambda s: None)
    assert fresh.get_json(f"{CR}/works/10.1/x") == {"ok": 1}
    assert route.call_count == 2


@respx.mock
def test_persistent_failure_raises_lookup_failed(http):
    respx.get(f"{CR}/works/10.1/y").mock(return_value=httpx.Response(500))
    with pytest.raises(LookupFailed):
        http.get_json(f"{CR}/works/10.1/y")


# -- per-reference labels ------------------------------------------------------------
@respx.mock
def test_doi_verified(http):
    respx.get(f"{CR}/works/10.1038/nature14539").mock(return_value=httpx.Response(
        200, json=crossref_msg("Deep learning", 2015, "LeCun")))
    respx.get(f"{CR}/works").mock(return_value=httpx.Response(200, json=NO_UPDATES))
    ref = Reference(title="Deep Learning", year=2015, doi="10.1038/nature14539", authors=["LeCun, Y"])
    assert R.check_reference(1, ref, http).label == "verified"


@respx.mock
def test_doi_mismatch(http):
    respx.get(f"{CR}/works/10.1234/fake.5678").mock(return_value=httpx.Response(
        200, json=crossref_msg("Soil bacteria of the Atacama desert", 2003, "Garcia")))
    respx.get(f"{CR}/works").mock(return_value=httpx.Response(200, json=NO_UPDATES))
    ref = Reference(title="Quantum gravity in a teacup", year=2021, doi="10.1234/fake.5678", authors=["Nobody, A"])
    rc = R.check_reference(1, ref, http)
    assert rc.label == "mismatch" and rc.found_title.startswith("Soil")


@respx.mock
def test_doi_retracted_via_updated_by(http):
    respx.get(f"{CR}/works/10.1/r").mock(return_value=httpx.Response(200, json=crossref_msg(
        "Some study", 2010, "Doe", **{"updated-by": [{"type": "retraction", "DOI": "10.1/notice"}]})))
    rc = R.check_reference(1, Reference(title="Some study", doi="10.1/r"), http)
    assert rc.label == "retracted" and "10.1/notice" in rc.reason


@respx.mock
def test_doi_retracted_via_updates_filter(http):
    respx.get(f"{CR}/works/10.1/r2").mock(return_value=httpx.Response(200, json=crossref_msg("Study", 2010, "Doe")))
    respx.get(f"{CR}/works", params={"filter": "updates:10.1/r2"}).mock(return_value=httpx.Response(200, json={
        "message": {"items": [{"DOI": "10.1/notice2", "update-to": [{"type": "retraction", "DOI": "10.1/R2"}]}]}}))
    assert R.check_reference(1, Reference(title="Study", doi="10.1/r2"), http).label == "retracted"


@respx.mock
def test_doi_not_registered_anywhere(http):
    respx.get(f"{CR}/works/10.1/nope").mock(return_value=httpx.Response(404))
    respx.get(f"{OA}/works/doi:10.1/nope").mock(return_value=httpx.Response(404))
    respx.get("https://doi.org/api/handles/10.1/nope").mock(return_value=httpx.Response(
        404, json={"responseCode": 100}))
    assert R.check_reference(1, Reference(title="Ghost paper title", doi="10.1/nope"), http).label == "not_found"


@respx.mock
def test_arxiv_doi_resolved_via_openalex(http):
    doi = "10.48550/arxiv.1706.03762"
    respx.get(f"{CR}/works/{doi}").mock(return_value=httpx.Response(404))
    respx.get(f"{OA}/works/doi:{doi}").mock(return_value=httpx.Response(200, json={
        "display_name": "Attention Is All You Need", "publication_year": 2017, "is_retracted": False}))
    rc = R.check_reference(1, Reference(title="Attention is all you need", year=2017, doi=doi), http)
    assert rc.label == "verified" and rc.source == "openalex"


@respx.mock
def test_title_search_requires_close_match(http):
    respx.get(f"{OA}/works").mock(return_value=httpx.Response(200, json={"results": [
        {"display_name": "Attention is all you need", "publication_year": 2017,
         "doi": "https://doi.org/10.5555/x", "is_retracted": False}]}))
    rc = R.check_reference(1, Reference(title="Attention is all you need", year=2017), http)
    assert rc.label == "verified" and rc.found_doi == "10.5555/x"
    rc = R.check_reference(2, Reference(title="Attention is mostly what you might need sometimes"), http)
    assert rc.label == "not_found"


@respx.mock
def test_lookup_failure_is_unverifiable_not_not_found(http):
    respx.get(f"{OA}/works").mock(return_value=httpx.Response(503))
    rc = R.check_reference(1, Reference(title="A perfectly fine paper title"), http)
    assert rc.label == "unverifiable" and "Lookup failed" in rc.reason


def test_books_web_and_short_titles_unverifiable(http):
    assert R.check_reference(1, Reference(kind="web", urls=["https://x.org"]), http).label == "unverifiable"
    assert R.check_reference(2, Reference(title="Ibid"), http).label == "unverifiable"


@respx.mock
def test_book_not_found_is_unverifiable(http):
    respx.get(f"{OA}/works").mock(return_value=httpx.Response(200, json={"results": []}))
    rc = R.check_reference(1, Reference(title="Pattern Recognition and Machine Learning", kind="book"), http)
    assert rc.label == "unverifiable"


# -- URLs ------------------------------------------------------------------------------
@respx.mock
def test_url_alive_dead_and_wayback(http):
    respx.head("https://ok.org/a").mock(return_value=httpx.Response(200))
    assert R.check_url("https://ok.org/a", http).alive
    respx.head("https://nohead.org/a").mock(return_value=httpx.Response(405))
    respx.get("https://nohead.org/a").mock(return_value=httpx.Response(200))
    assert R.check_url("https://nohead.org/a", http).alive
    respx.head("https://gone.org/a").mock(return_value=httpx.Response(404))
    respx.get("https://gone.org/a").mock(return_value=httpx.Response(404))
    respx.get("https://archive.org/wayback/available").mock(return_value=httpx.Response(200, json={
        "archived_snapshots": {"closest": {"available": True, "url": "http://web.archive.org/web/2020/gone"}}}))
    u = R.check_url("https://gone.org/a", http)
    assert not u.alive and u.status == 404 and u.archived.endswith("/gone")


# -- traffic light ---------------------------------------------------------------------
def _rc(label):
    return R.RefCheck(index=1, label=label, reason="", reference=Reference(title="t"))


def _urls(alive, dead):
    return [R.UrlCheck("u", True)] * alive + [R.UrlCheck("d", False)] * dead


@pytest.mark.parametrize("labels,urls,expected", [
    ([], [], Status.GREY),
    (["verified"] * 10, [], Status.GREEN),
    (["verified"] * 9 + ["retracted"], [], Status.RED),
    (["verified"] * 8 + ["not_found", "mismatch"], [], Status.RED),
    (["verified"] * 9 + ["not_found"], [], Status.YELLOW),
    (["verified"] * 10, _urls(8, 2), Status.YELLOW),     # 20% dead
    (["verified"] * 10, _urls(10, 1), Status.GREEN),     # 9% dead
    (["verified"] * 4 + ["unverifiable"] * 6, [], Status.GREY),
    (["verified"] * 5 + ["unverifiable"] * 5, [], Status.GREEN),  # exactly 50% is not > 50%
])
def test_verdict_rules(labels, urls, expected):
    assert verdict([_rc(lab) for lab in labels], urls)[0] == expected


@respx.mock
def test_end_to_end_on_fixture(http):
    """Fixture: 1 verified DOI, 1 mismatched DOI, 1 found by title, 1 book, 1 dead URL."""
    respx.get(f"{CR}/works/10.1038/nature14539").mock(return_value=httpx.Response(200, json=DEEP_LEARNING))
    respx.get(f"{CR}/works/10.1234/fake.5678").mock(return_value=httpx.Response(
        200, json=crossref_msg("Soil bacteria of the Atacama desert", 2003, "Garcia")))
    respx.get(f"{CR}/works").mock(return_value=httpx.Response(200, json=NO_UPDATES))
    respx.get(f"{OA}/works").mock(side_effect=lambda req: httpx.Response(200, json={"results": [
        {"display_name": "Attention is All you Need", "publication_year": 2017,
         "authorships": [{"author": {"display_name": "Ashish Vaswani"}}]}]
        if "Attention" in req.url.params["search"] else []}))
    respx.head("https://example.org/dataset").mock(return_value=httpx.Response(404))
    respx.get("https://example.org/dataset").mock(return_value=httpx.Response(404))
    respx.get("https://archive.org/wayback/available").mock(return_value=httpx.Response(
        200, json={"archived_snapshots": {}}))

    paper = grobid.parse_tei(FIXTURE.read_text())
    result = check_references(paper, http)
    labels = [r["label"] for r in result.stats["references"]]
    assert labels == ["verified", "mismatch", "verified", "unverifiable", "unverifiable"]
    # one mismatch -> yellow; plus 1 of 1 URL dead
    assert result.status == Status.YELLOW
    kinds = {e.label for e in result.evidence}
    assert {"mismatch", "dead_url"} <= kinds
    json.loads(result.to_json())


def test_no_anthropic_in_reference_checker():
    pkg = Path(R.__file__).parents[1]
    for f in ["checks/references.py", "ingest/grobid.py", "ingest/doi.py", "net.py", "check_references.py"]:
        assert "anthropic" not in (pkg / f).read_text()


# -- strict matching: exact title + authors / journal / year / volume / pages -----------
def test_norm_ignores_case_punctuation_accents_and_tags():
    assert R.norm("Schrödinger's <i>Cat</i>: a  Study.") == R.norm("schrodinger s cat a study")


@pytest.mark.parametrize("ref,label,field", [
    (Reference(title="Deep learning", authors=["LeCun, Y", "Bengio, Y"], venue="Nature", year=2015,
               volume="521", first_page="436"), "verified", None),
    (Reference(title="Deep learning.", authors=["LeCun, Y"], venue="Nature"), "verified", None),
    (Reference(title="Deep learning methods", authors=["LeCun, Y"]), "discrepancy", "title"),
    (Reference(title="Deep learning", authors=["LeCun, Y", "Smith, J"]), "discrepancy", "authors"),
    (Reference(title="Deep learning", authors=["Bengio, Y", "LeCun, Y"]), "discrepancy", "authors"),
    (Reference(title="Deep learning", venue="Science"), "discrepancy", "journal"),
    (Reference(title="Deep learning", year=2012), "discrepancy", "year"),
    (Reference(title="Deep learning", volume="512"), "discrepancy", "volume"),
    (Reference(title="Deep learning", first_page="999"), "discrepancy", "first_page"),
])
@respx.mock
def test_doi_field_comparison(http, ref, label, field):
    respx.get(f"{CR}/works/10.1038/nature14539").mock(return_value=httpx.Response(200, json=DEEP_LEARNING))
    respx.get(f"{CR}/works").mock(return_value=httpx.Response(200, json=NO_UPDATES))
    ref.doi = "10.1038/nature14539"
    rc = R.check_reference(1, ref, http)
    assert rc.label == label, rc.reason
    differs = [f["field"] for f in rc.fields if f["status"] == "differs"]
    assert differs == ([field] if field else [])


def test_journal_abbreviations_match():
    full = ["Physical Review Letters"]
    assert R._venue_matches("Phys. Rev. Lett.", full)
    assert R._venue_matches("Physical review letters", full)
    assert not R._venue_matches("Phys. Rev. D", full)


@respx.mock
def test_title_search_near_match_is_discrepancy(http):
    respx.get(f"{OA}/works").mock(return_value=httpx.Response(200, json={"results": [
        {"display_name": "Attention is all you need", "publication_year": 2017,
         "authorships": [{"author": {"display_name": "Ashish Vaswani"}}]}]}))
    rc = R.check_reference(1, Reference(title="Attention is all you needed", authors=["Vaswani, A"]), http)
    assert rc.label == "discrepancy" and rc.fields[0]["field"] == "title"


@respx.mock
def test_same_title_other_authors_and_year_is_not_found(http):
    respx.get(f"{OA}/works").mock(return_value=httpx.Response(200, json={"results": [
        {"display_name": "Attention is all you need", "publication_year": 2017,
         "authorships": [{"author": {"display_name": "Ashish Vaswani"}}]}]}))
    rc = R.check_reference(1, Reference(title="Attention is all you need", authors=["Smith, J"], year=2023), http)
    assert rc.label == "not_found" and "different authors" in rc.reason


def test_verdict_discrepancy_fraction():
    assert verdict([_rc("verified")] * 9 + [_rc("discrepancy")], [])[0] == Status.GREEN   # 10%
    assert verdict([_rc("verified")] * 8 + [_rc("discrepancy")] * 2, [])[0] == Status.YELLOW


def test_verdict_any_author_discrepancy_is_yellow():
    wrong_authors = _rc("discrepancy")
    wrong_authors.fields = [{"field": "authors", "status": "differs", "cited": "Smith", "found": "Jones"}]
    status, summary = verdict([_rc("verified")] * 49 + [wrong_authors], [])   # 2%, still yellow
    assert status == Status.YELLOW and "authors" in summary
