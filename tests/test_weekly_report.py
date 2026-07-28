"""Phase 6 每周情报报告测试：db 聚合查询 / 统计 / LLM 趋势总结 / HTML 组装；B3 月报阅读趋势。"""

import sys
from datetime import date, timedelta

import pytest

import weekly_report
from database.db import (
    connect, get_feedback_since, get_week_recommendations, save_feedback,
    save_news_summary, save_paper, save_recommendation, upsert_learned_term,
)
from feedback.vocab import load_active_terms
from mailer.weekly_builder import build_weekly_html
from processing.weekly_stats import (
    compute_reading_trends, compute_stats, normalize_feedback_value,
)
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
    trends = {"feedback": {"positive": 1, "neutral": 0, "negative": 0, "total": 1},
              "top_terms": [("bee", 1.5)]}
    html = build_weekly_html("张三<", "2026-07-16", "2026-07-22", rows,
                             "趋势 <总结>", stats, trends)
    assert "张三&lt;" in html
    assert "趋势 &lt;总结&gt;" in html
    assert "Title &lt;with&gt; tags" in html
    assert "新闻 &lt;b&gt; 解读" in html
    assert "Ref 不列入清单" not in html          # 清单只含 Must Read / Important
    assert "共 2 篇" in html
    assert "顶刊 1 篇" in html
    assert "2026-07-16 ~ 2026-07-22" in html
    assert "共 1 条（正向 1 / 中性 0 / 负向 0）" in html  # 阅读趋势块
    assert "bee（1.50）" in html


def test_build_weekly_html_fallback_when_no_trend_summary():
    stats = compute_stats([_row("Reference", "X", "[]")], {})
    trends = {"feedback": {"positive": 0, "neutral": 0, "negative": 0, "total": 0},
              "top_terms": []}
    html = build_weekly_html("User", "2026-07-16", "2026-07-22",
                             [_row("Reference", "X", "[]")], "", stats, trends)
    assert "本周高价值论文较少，未生成趋势总结。" in html
    assert "本周没有 Must Read / Important 论文" in html
    assert "共 0 条（正向 0 / 中性 0 / 负向 0）" in html
    assert '<td class="stats-label">学习热词</td>' not in html  # 无有效学习词时不渲染该行


# ---------- B3 阅读趋势：反馈归一化（兼容旧四值与新五星） ----------

def test_normalize_feedback_value_legacy_four_values():
    assert normalize_feedback_value("relevant") == "positive"
    assert normalize_feedback_value("save") == "positive"
    assert normalize_feedback_value("already_read") == "neutral"
    assert normalize_feedback_value("not_relevant") == "negative"


def test_normalize_feedback_value_five_star_values():
    assert normalize_feedback_value("5") == "positive"
    assert normalize_feedback_value("4") == "positive"
    assert normalize_feedback_value("3") == "neutral"
    assert normalize_feedback_value("2") == "negative"
    assert normalize_feedback_value("1") == "negative"


def test_normalize_feedback_value_unknown_returns_none():
    assert normalize_feedback_value("whatever") is None
    assert normalize_feedback_value("") is None
    assert normalize_feedback_value(None) is None
    # 大小写与首尾空白容忍
    assert normalize_feedback_value(" Relevant ") == "positive"


def test_get_feedback_since_filters_by_user_and_window(conn):
    pid = save_paper(conn, _paper("10.1/fb"))
    save_feedback(conn, "a@x.com", pid, "relevant")      # created_time = 现在
    save_feedback(conn, "b@x.com", pid, "not_relevant")  # 其他用户
    conn.execute(
        "INSERT INTO feedback (user_email, paper_id, value, reason, created_time)"
        " VALUES (?, ?, ?, ?, ?)",
        ("a@x.com", pid, "save", "", "2026-06-01T00:00:00+00:00"),  # 窗口外
    )
    conn.commit()

    rows = get_feedback_since(conn, "a@x.com", "2026-07-01")
    assert [r["value"] for r in rows] == ["relevant"]
    rows_all = get_feedback_since(conn, "a@x.com", "2026-05-01")
    assert [r["value"] for r in rows_all] == ["save", "relevant"]  # 按时间升序


def test_compute_reading_trends_buckets_and_top_terms(conn):
    pid = save_paper(conn, _paper("10.1/trend"))
    # 旧四值与新五星混合：正 3 / 中 2 / 负 2
    for value in ["relevant", "5", "save", "already_read", "3", "not_relevant", "1"]:
        save_feedback(conn, "a@x.com", pid, value)
    upsert_learned_term(conn, "a@x.com", "hot", 2.0, 5, "2026-07-23")
    upsert_learned_term(conn, "a@x.com", "cold", 0.4, 2, "2026-06-23")  # 衰减后 0.2 失效

    feedback_rows = get_feedback_since(conn, "a@x.com", "2026-07-01")
    terms = load_active_terms(conn, "a@x.com", today=date(2026, 7, 23))
    trends = compute_reading_trends(feedback_rows, terms)

    assert trends["feedback"] == {"positive": 3, "neutral": 2, "negative": 2, "total": 7}
    assert trends["top_terms"] == [("hot", 2.0)]  # cold 衰减失效被过滤


def test_compute_reading_trends_skips_unknown_values():
    rows = [{"value": "relevant"}, {"value": "???"}, {"value": ""}, {"value": "2"}]
    trends = compute_reading_trends(rows, [])
    # 无法识别的值跳过且不计入总数
    assert trends["feedback"] == {"positive": 1, "neutral": 0, "negative": 1, "total": 2}
    assert trends["top_terms"] == []


def test_monthly_report_end_to_end_days_30(tmp_path, monkeypatch):
    """--days 30 月报链路：聚合 30 天推荐 + 反馈/学习词 → 渲染阅读趋势块 → 投递。"""
    db_path = tmp_path / "test.db"
    c = connect(db_path)
    today = date.today()
    recent = (today - timedelta(days=10)).isoformat()
    old = (today - timedelta(days=40)).isoformat()

    pid = save_paper(c, _paper("10.1/m", title="Monthly Must", journal="Nature"))
    save_recommendation(c, "a@x.com", pid, "Must Read", 9, recent)
    save_news_summary(c, pid, "月度新闻解读")
    old_pid = save_paper(c, _paper("10.1/old", title="Old Out Of Window"))
    save_recommendation(c, "a@x.com", old_pid, "Must Read", 9, old)

    save_feedback(c, "a@x.com", pid, "relevant")
    save_feedback(c, "a@x.com", pid, "5")
    # 窗口外的反馈不计入阅读趋势
    c.execute(
        "INSERT INTO feedback (user_email, paper_id, value, reason, created_time)"
        " VALUES (?, ?, ?, ?, ?)",
        ("a@x.com", pid, "not_relevant", "", f"{old}T00:00:00+00:00"),
    )
    c.commit()
    upsert_learned_term(c, "a@x.com", "crispr", 2.0, 5, today.isoformat())
    c.close()

    sent = {}
    monkeypatch.setattr(weekly_report, "connect", lambda: connect(db_path))
    monkeypatch.setattr(weekly_report, "load_users",
                        lambda: [("user001", {"name": "张三", "email": "a@x.com"})])
    monkeypatch.setattr(weekly_report, "LLMClient", FakeLLM)
    monkeypatch.setattr(weekly_report, "send_email",
                        lambda to, subject, html: sent.update(to=to, subject=subject, html=html))
    monkeypatch.setattr(weekly_report, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["weekly_report.py", "--days", "30"])

    assert weekly_report.main() == 0
    assert sent["to"] == "a@x.com"
    since = (today - timedelta(days=30)).isoformat()
    assert f"{since} ~ {today.isoformat()}" in sent["subject"]
    html = sent["html"]
    assert "Monthly Must" in html
    assert "Old Out Of Window" not in html             # 30 天窗口外的推荐不聚合
    assert "本周趋势总结文本" in html                     # mock LLM 的趋势总结
    assert "阅读趋势" in html
    assert "共 2 条（正向 2 / 中性 0 / 负向 0）" in html   # 窗口外 not_relevant 被排除
    assert "crispr（2.00）" in html
