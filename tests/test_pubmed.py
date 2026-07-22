from pathlib import Path

import pytest

from sources.paper import Paper
from sources.pubmed import build_queries, dedupe, parse_efetch_xml

FIXTURE = Path(__file__).parent / "fixtures" / "pubmed_efetch.xml"


@pytest.fixture(scope="module")
def papers():
    return parse_efetch_xml(FIXTURE.read_text(encoding="utf-8"))


def test_parse_returns_all_articles(papers):
    assert len(papers) == 2


def test_parse_first_article_fields(papers):
    p = papers[0]
    assert p.title.startswith("A single-cell transcriptomic atlas")
    assert p.authors == "Zhang Wei, Li Na"
    assert p.journal == "Nature Communications"
    assert p.date == "2025-07-16"  # 来自 ArticleDate
    assert p.doi == "10.1038/s41467-025-00001-x"
    assert p.url == "https://pubmed.ncbi.nlm.nih.gov/40123456/"
    assert p.keywords == ["snRNA-seq", "Apis mellifera"]


def test_structured_abstract_sections_joined(papers):
    assert "poorly characterized" in papers[0].abstract
    assert "87 clusters" in papers[0].abstract


def test_parse_pubdate_english_month(papers):
    # 第二篇没有 ArticleDate，应回退解析 PubDate 的英文月份
    assert papers[1].date == "2025-07-18"


def _paper(title, doi=""):
    return Paper(title=title, abstract="", authors="", journal="",
                 date="", doi=doi, url="")


def test_dedupe_by_doi_case_insensitive():
    a = _paper("Title A", doi="10.1/ABC")
    b = _paper("Title B", doi="10.1/abc")
    assert dedupe([a, b]) == [a]


def test_dedupe_by_normalized_title_when_no_doi():
    a = _paper("Same   Title")
    b = _paper("same title")
    assert dedupe([a, b]) == [a]


def test_build_queries_strict_combines_species_and_others():
    user = {
        "research_interest": ["insect evolution"],
        "keywords": [],
        "methods": ["single-cell RNA sequencing"],
        "species": ["Apis", "Bombus"],
        "exclude": [],
    }
    strict, relaxed = build_queries(user)
    assert strict == '("Apis" OR "Bombus") AND ("insect evolution" OR "single-cell RNA sequencing")'
    assert relaxed == '"Apis" OR "Bombus" OR "insect evolution" OR "single-cell RNA sequencing"'


def test_build_queries_appends_not_for_exclude():
    user = {"research_interest": ["single cell"], "species": ["Apis"],
            "exclude": ["cancer", "tumor"]}
    strict, relaxed = build_queries(user)
    assert strict.endswith('NOT ("cancer" OR "tumor")')
    assert relaxed.endswith('NOT ("cancer" OR "tumor")')


def test_build_queries_without_species_falls_back_to_flat_or():
    user = {"research_interest": ["insect evolution"], "methods": ["scRNA-seq"]}
    strict, relaxed = build_queries(user)
    assert strict == relaxed == '"insect evolution" OR "scRNA-seq"'


def test_build_queries_empty_terms_raises():
    with pytest.raises(ValueError):
        build_queries({"research_interest": [], "keywords": []})


def test_build_queries_expands_aliases_into_or_groups():
    user = {
        "research_interest": ["insect evolution"],
        "species": ["honeybee"],
        "exclude": [],
        "aliases": {"honeybee": ["Apis mellifera", "honeybee"]},  # 重复别名应去重
    }
    strict, relaxed = build_queries(user)
    assert strict == '("honeybee" OR "Apis mellifera") AND ("insect evolution")'
    assert relaxed == '"honeybee" OR "Apis mellifera" OR "insect evolution"'
    assert relaxed.count('"honeybee"') == 1  # 大小写不敏感去重，原词只出现一次
