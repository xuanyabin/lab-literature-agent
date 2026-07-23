"""Phase 6 每周情报报告测试：db 聚合查询 / 统计 / LLM 趋势总结 / HTML 组装。"""

import pytest

from database.db import (
    connect, get_week_recommendations, save_news_summary, save_paper,
    save_recommendation,
)
from mailer.weekly_builder import build_weekly_html
from processing.weekly_stats import compute_stats
from processing.weekly_summary_generator import generate_weekly_summary
from sources.paper import Paper


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def _paper(doi, title="A Title", journal="Cell", keywords=None):
    return Paper(title=title, abstract="abs", authors="Zhang", journal=journal,
                 date="2026-07-22", doi=doi, url="http://x",
                 keywords=keywords if keywords is not None else ["kw1"])


def _seed_week(conn, user="a@x.com"):
    """造三篇不同定级/日期的推荐记录，返回 {category: paper_id}。"""
    ids = {}
    for doi, category, score, sent in [
        ("10.1/must", "Must Read", 9, "2026-07-21"),
        ("10.1/imp", "Important", 6, "2026-07-22"),
        ("10.1/ref", "Reference", 2, "2026-07-22"),
    ]:
        pid = save_paper(conn, _paper(doi, title=f"Paper {category}"))
        save_recommendation(conn, user, pid, category, score, sent)
        ids[category] = pid
    return ids


def test_week_recommendations_filters_by_since_and_user(conn):
    ids = _seed_week(conn)
    old = save_paper(conn, _paper("10.1/old", title="Old Paper"))
    save_recommendation(conn, "a@x.com", old, "Must Read", 9, "2026-07-10")
    other = save_paper(conn, _paper("10.1/other", title="Other User Paper"))
    save_recommendation(conn, "b@x.com", other, "Must Read", 9, "2026-07-22")

    rows = get_week_recommendations(conn, "a@x.com", "2026-07-20")
    titles = [r["title"] for r in rows]
    assert titles == ["Paper Must Read", "Paper Important", "Paper Reference"]
    assert "Old Paper" not in titles          # since 之前的被过滤
    assert "Other User Paper" not in titles   # 其他用户互不影响

    rows_b = get_week_recommendations(conn, "b@x.com", "2026-07-20")
    assert [r["title"] for r in rows_b] == ["Other User Paper"]


def test_week_recommendations_orders_by_category_then_score(conn):
    for doi, category, score in [
        ("10.1/i1", "Important", 8),
        ("10.1/m1", "Must Read", 5),
        ("10.1/i2", "Important", 6),
    ]:
        pid = save_paper(conn, _paper(doi, title=f"{category} {score}"))
        save_recommendation(conn, "a@x.com", pid, category, score, "2026-07-22")
    rows = get_week_recommendations(conn, "a@x.com", "2026-07-01")
    assert [(r["category"], r["score"]) for r in rows] == [
        ("Must Read", 5), ("Important", 8), ("Important", 6),
    ]


def test_week_recommendations_joins_news_summary(conn):
    ids = _seed_week(conn)
    save_news_summary(conn, ids["Must Read"], "这是新闻解读")
    rows = get_week_recommendations(conn, "a@x.com", "2026-07-01")
    news = {r["category"]: r["news"] for r in rows}
    assert news["Must Read"] == "这是新闻解读"
    assert news["Reference"] is None  # 无解读时为 None（LEFT JOIN）


def _row(category, journal, keywords, title="T", news="n", sent_date="2026-07-22"):
    return {"category": category, "journal": journal, "keywords": keywords,
            "title": title, "news": news, "sent_date": sent_date,
            "date": "2026-07-22", "url": "http://x", "score": 5}


def test_compute_stats_counts_categories_tiers_journals_keywords():
    rows = [
        _row("Must Read", "Nature", '["bee", "dance"]'),
        _row("Must Read", "Nature", '["bee"]'),
        _row("Important", "Cell", '["dance"]'),
        _row("Reference", "Some Journal", '["other"]'),
        _row("Reference", "Some Journal", "not json"),
    ]
    stats = compute_stats(rows, {"nature": "t0", "cell": "t1"})
    assert stats["total"] == 5
    assert stats["by_category"] == {"Must Read": 2, "Important": 1, "Reference": 2}
    assert stats["by_tier"] == {"t0": 2, "t1": 1, "other": 2}
    assert stats["top_journals"][0] == ("Nature", 2)
    assert ("Some Journal", 2) in stats["top_journals"]
    assert stats["top_keywords"][0] == ("bee", 2)
    assert ("dance", 2) in stats["top_keywords"]


class FakeLLM:
    def __init__(self):
        self.prompt = None

    def complete(self, prompt):
        self.prompt = prompt
        return "  本周趋势总结文本  "


def test_weekly_summary_uses_only_high_categories():
    rows = [
        _row("Must Read", "Nature", "[]", title="Must Title"),
        _row("Important", "Cell", "[]", title="Imp Title"),
        _row("Reference", "X", "[]", title="Ref Title"),
    ]
    llm = FakeLLM()
    summary = generate_weekly_summary(rows, llm)
    assert summary == "本周趋势总结文本"
    assert "Must Title" in llm.prompt
    assert "Imp Title" in llm.prompt
    assert "Ref Title" not in llm.prompt  # Reference 不进入趋势总结


def test_weekly_summary_empty_when_no_high_categories():
    assert generate_weekly_summary([_row("Reference", "X", "[]")], FakeLLM()) == ""


def test_build_weekly_html_renders_and_escapes():
    rows = [
        _row("Must Read", "Nature", '["bee"]', title="Title <with> tags",
             news="新闻 <b> 解读"),
        _row("Reference", "Cell", "[]", title="Ref 不列入清单"),
    ]
    stats = compute_stats(rows, {"nature": "t0"})
    html = build_weekly_html("张三<", "2026-07-16", "2026-07-22", rows,
                             "趋势 <总结>", stats)
    assert "张三&lt;" in html
    assert "趋势 &lt;总结&gt;" in html
    assert "Title &lt;with&gt; tags" in html
    assert "新闻 &lt;b&gt; 解读" in html
    assert "Ref 不列入清单" not in html          # 清单只含 Must Read / Important
    assert "共 2 篇" in html
    assert "顶刊 1 篇" in html
    assert "2026-07-16 ~ 2026-07-22" in html


def test_build_weekly_html_fallback_when_no_trend_summary():
    stats = compute_stats([_row("Reference", "X", "[]")], {})
    html = build_weekly_html("User", "2026-07-16", "2026-07-22",
                             [_row("Reference", "X", "[]")], "", stats)
    assert "本周高价值论文较少，未生成趋势总结。" in html
    assert "本周没有 Must Read / Important 论文" in html
