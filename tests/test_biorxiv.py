import json
from pathlib import Path

import pytest

import sources.biorxiv as biorxiv

FIXTURE = Path(__file__).parent / "fixtures" / "biorxiv_details.json"

USER = {
    "species": ["honeybee"],
    "aliases": {"honeybee": ["Apis mellifera"]},
    "research_interest": [],
    "keywords": [],
    "methods": ["single-cell RNA sequencing"],
}


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    biorxiv._DETAILS_CACHE.clear()
    yield
    biorxiv._DETAILS_CACHE.clear()


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _mock_get(monkeypatch, payload):
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResp(payload)

    monkeypatch.setattr(biorxiv.requests, "get", fake_get)
    return calls


def test_fetch_recent_filters_and_parses(monkeypatch, payload):
    _mock_get(monkeypatch, payload)
    papers = biorxiv.fetch_recent(USER, days=1)
    # 第 1 篇物种（别名）+ 方法双命中入选；第 2 篇零命中被过滤
    assert len(papers) == 1
    p = papers[0]
    assert p.journal == "bioRxiv"
    assert p.doi == "10.1101/2026.07.20.123456"
    assert p.url == "https://www.biorxiv.org/content/10.1101/2026.07.20.123456"
    assert p.authors == "Dutta S., Zhang W."
    assert p.date == "2026-07-21"
    assert p.keywords == []


def test_relaxed_fallback_when_strict_too_few(monkeypatch, payload):
    _mock_get(monkeypatch, payload)
    # 物种词只命中标题/摘要的一部分，无法与其余词组同时命中 → 严格为空，降级宽松
    user = {"species": ["honeybee"], "research_interest": ["genomics"]}
    papers = biorxiv.fetch_recent(user, days=1)
    assert [p.title for p in papers] == ["Honeybee gut microbiome shifts under pesticide stress"]


def test_strict_preferred_when_enough_hits(monkeypatch, payload):
    _mock_get(monkeypatch, payload)
    # 给两篇都加上严格命中所需的双组词，min_results=1 时不混入宽松命中
    payload["collection"][1]["abstract"] = "A honeybee study using genomics."
    user = {"species": ["honeybee"], "research_interest": ["genomics"]}
    papers = biorxiv.fetch_recent(user, days=1, min_results=1)
    assert all("honeybee" in (p.title + p.abstract).lower() for p in papers)
    assert len(papers) == 1  # 只有第 2 篇双组命中；第 1 篇无 genomics 不入选


def test_exclude_term_drops_paper(monkeypatch, payload):
    _mock_get(monkeypatch, payload)
    user = {**USER, "exclude": ["pesticide"]}
    assert biorxiv.fetch_recent(user, days=1) == []


def test_learned_terms_participate_in_filter(monkeypatch, payload):
    _mock_get(monkeypatch, payload)
    # 学习词 microbiome 只出现在第 1 篇标题中
    user = {"research_interest": ["genomics"], "learned_terms": [("microbiome", 1.5)]}
    papers = biorxiv.fetch_recent(user, days=1)
    assert [p.title for p in papers] == ["Honeybee gut microbiome shifts under pesticide stress"]


def test_pagination_follows_cursor(monkeypatch):
    page1 = {"messages": [{"total": "3"}],
             "collection": [{"title": f"p{i}", "authors": "", "doi": f"10.1/{i}",
                             "date": "2026-07-22", "category": "", "abstract": "honeybee"}
                            for i in range(2)]}
    page2 = {"messages": [{"total": "3"}],
             "collection": [{"title": "p2", "authors": "", "doi": "10.1/2",
                             "date": "2026-07-22", "category": "", "abstract": "honeybee"}]}
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResp(page2 if url.endswith("/2") else page1)

    monkeypatch.setattr(biorxiv.requests, "get", fake_get)
    papers = biorxiv.fetch_recent({"species": ["honeybee"]}, days=1)
    assert len(papers) == 3
    assert len(calls) == 2 and calls[1].endswith("/2")


def test_details_cached_across_users(monkeypatch, payload):
    calls = _mock_get(monkeypatch, payload)
    biorxiv.fetch_recent(USER, days=1)
    biorxiv.fetch_recent({"species": ["unrelated"]}, days=1)
    assert len(calls) == 1  # 第二个用户共享同一天区间的缓存，不再请求


def test_api_error_returns_empty(monkeypatch):
    def fake_get(url, timeout=None):
        raise ConnectionError("boom")

    monkeypatch.setattr(biorxiv.requests, "get", fake_get)
    assert biorxiv.fetch_recent(USER, days=1) == []


def test_no_terms_returns_empty(monkeypatch, payload):
    calls = _mock_get(monkeypatch, payload)
    assert biorxiv.fetch_recent({"species": []}, days=1) == []
    assert calls == []  # 无检索词时不发起请求


def test_fetch_recent_global_direct_terms(monkeypatch, payload):
    _mock_get(monkeypatch, payload)
    # 词表已合并展开好直接传入：大小写不敏感，命中任一词即收录（仅第 1 篇命中）
    papers = biorxiv.fetch_recent_global(
        ["Honeybee", "Apis mellifera"], ["single-cell RNA sequencing"], days=1)
    assert [p.doi for p in papers] == ["10.1101/2026.07.20.123456"]


def test_fetch_recent_global_flat_or_any_hit(monkeypatch, payload):
    _mock_get(monkeypatch, payload)
    # 扁平 OR：第 1 篇命中 microbiome、第 2 篇命中 mouse，两篇都收录（保持抓取顺序）
    papers = biorxiv.fetch_recent_global(["mouse"], ["microbiome"], days=1)
    assert [p.doi for p in papers] == [
        "10.1101/2026.07.20.123456", "10.1101/2026.07.20.654321"]
