from sources import top_journals
from sources.paper import Paper


def _paper(pmid: str, journal: str) -> Paper:
    return Paper(
        title=f"Paper {pmid}",
        authors="A",
        abstract="x",
        journal=journal,
        date="2026-07-29",
        doi=f"10.1/{pmid}",
        url="",
    )


def test_load_journal_names_by_tier(tmp_path):
    cfg = tmp_path / "journals.yaml"
    cfg.write_text(
        "t0:\n"
        "  - Nature\n"
        "  - Science\n"
        "t1:\n"
        "  - Cell Reports\n",
        encoding="utf-8",
    )
    assert top_journals.load_journal_names(cfg, tiers=("t0",)) == ["Nature", "Science"]
    assert top_journals.load_journal_names(cfg, tiers=("t0", "t1")) == [
        "Nature",
        "Science",
        "Cell Reports",
    ]
    assert top_journals.load_journal_names(tmp_path / "missing.yaml") == []


def test_fetch_top_journals_merges_and_tolerates_failures(monkeypatch):
    queries = []

    def fake_search(query, days, retmax=200, datetype="pdat"):
        queries.append((query, days, retmax))
        if "bad" in query.lower():
            raise ConnectionError("pubmed down")
        return {"nature": ["1", "2"], "science": ["3"]}.get(
            query.split("[jour]")[0].strip('"').lower(), [])

    def fake_fetch(pmids):
        return [_paper(p, "Journal") for p in pmids]

    monkeypatch.setattr(top_journals.pubmed, "search_pmids", fake_search)
    monkeypatch.setattr(top_journals.pubmed, "fetch_by_pmids", fake_fetch)
    monkeypatch.setattr(top_journals.time, "sleep", lambda *_a, **_k: None)

    papers = top_journals.fetch_top_journals(
        ["Nature", "Bad Journal", "Science"], days=1, retmax_per_journal=5
    )

    assert ('"Nature"[jour]', 1, 5) in queries
    assert ('"Bad Journal"[jour]', 1, 5) in queries
    assert ('"Science"[jour]', 1, 5) in queries
    # Bad Journal failed but did not abort the others
    assert [p.doi for p in papers] == ["10.1/1", "10.1/2", "10.1/3"]


def test_fetch_top_journals_empty_names(monkeypatch):
    monkeypatch.setattr(
        top_journals.pubmed,
        "search_pmids",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not query")),
    )
    assert top_journals.fetch_top_journals([], days=1) == []


def test_fetch_top_journals_passes_datetype(monkeypatch):
    seen = {}

    def fake_search(query, days, retmax=200, datetype="pdat"):
        seen["datetype"] = datetype
        return []

    monkeypatch.setattr(top_journals.pubmed, "search_pmids", fake_search)
    monkeypatch.setattr(top_journals.time, "sleep", lambda *_a, **_k: None)
    top_journals.fetch_top_journals(["Nature"], days=1, datetype="edat")
    assert seen["datetype"] == "edat"


# ---------- Crossref 直采 ----------

def test_load_journal_issns_filters_by_tier(tmp_path):
    cfg = tmp_path / "journals.yaml"
    cfg.write_text(
        "t0:\n"
        "  - Nature\n"
        "t1:\n"
        "  - Cell Reports\n"
        "issn:\n"
        "  Nature: '0028-0836'\n"
        "  Cell Reports: '2211-1247'\n"
        "  Not In Tiers: '0000-0000'\n",
        encoding="utf-8",
    )
    assert top_journals.load_journal_issns(cfg, tiers=("t0",)) == {"Nature": "0028-0836"}
    assert top_journals.load_journal_issns(cfg, tiers=("t0", "t1")) == {
        "Nature": "0028-0836", "Cell Reports": "2211-1247",
    }
    assert top_journals.load_journal_issns(tmp_path / "missing.yaml") == {}


def _crossref_item(doi="10.1016/j.cell.2026.07.001", title="A complete genome"):
    return {
        "DOI": doi,
        "title": [title],
        "author": [{"family": "Zhang", "given": "Wei"}, {"family": "Li"}],
        "container-title": ["Cell"],
        "abstract": "<jats:p>We present  a genome.</jats:p>",
        "published": {"date-parts": [[2026, 8, 6]]},
        "type": "journal-article",
    }


def test_parse_crossref_items_fields():
    papers = top_journals.parse_crossref_items(
        [_crossref_item(title="A <b>complete</b> genome")], journal_fallback="cell")
    assert len(papers) == 1
    p = papers[0]
    assert p.title == "A complete genome"  # 标题内嵌 HTML 标签已剥除
    assert p.authors == "Zhang Wei, Li"
    assert p.journal == "Cell"
    assert p.date == "2026-08-06"
    assert p.doi == "10.1016/j.cell.2026.07.001"
    assert p.url == "https://doi.org/10.1016/j.cell.2026.07.001"
    assert p.abstract == "We present a genome."  # JATS 标签已剥除


def test_parse_crossref_items_skips_incomplete_and_falls_back():
    items = [
        {"DOI": "10.1/x"},                       # 无标题 → 跳过
        {"title": ["No DOI"]},                   # 无 DOI → 跳过
        {**_crossref_item(doi="10.1/y"), "container-title": [],
         "published": {}, "published-online": {"date-parts": [[2026, 8]]}},
    ]
    papers = top_journals.parse_crossref_items(items, journal_fallback="cell")
    assert len(papers) == 1
    assert papers[0].journal == "cell"       # 缺 container-title 时回退刊名
    assert papers[0].date == "2026-08-01"    # date-parts 缺日补 01


def test_fetch_crossref_journals_window_and_failure_tolerance(monkeypatch):
    calls = []

    def fake_works(issn, from_date, to_date, rows):
        calls.append((issn, from_date, to_date, rows))
        if issn == "bad":
            raise ConnectionError("crossref down")
        return [_crossref_item()]

    monkeypatch.setattr(top_journals, "fetch_crossref_works", fake_works)
    monkeypatch.setattr(top_journals.time, "sleep", lambda *_a, **_k: None)

    papers = top_journals.fetch_crossref_journals(
        {"cell": "0092-8674", "bad journal": "bad"}, days=3, rows_per_journal=7)

    issn, from_date, to_date, rows = calls[0]
    assert issn == "0092-8674" and rows == 7
    # 窗口起点对齐月初（Crossref 部分刊只登记年-月精度日期）
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    assert to_date == today.isoformat()
    assert from_date == (today - _td(days=3)).replace(day=1).isoformat()
    assert [p.doi for p in papers] == ["10.1016/j.cell.2026.07.001"]  # 失败刊不阻断
