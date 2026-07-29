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

    def fake_search(query, days, retmax=200):
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
