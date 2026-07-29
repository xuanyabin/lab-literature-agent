import sqlite3

import pytest

from database.db import (
    connect, dedup_key, get_analysis, get_news_summary, get_paper_id,
    get_recommendation_paper_ids, get_seen_keys, get_translation,
    save_analysis, save_news_summary, save_paper, save_recommendation,
    save_translation,
)
from sources.paper import Paper


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def _paper(doi="10.1/x", title="A Title"):
    return Paper(title=title, abstract="abs", authors="Zhang", journal="Cell",
                 date="2026-07-22", doi=doi, url="http://x", keywords=["kw1", "kw2"])


def test_dedup_key_prefers_doi():
    assert dedup_key(_paper()) == "doi:10.1/x"
    assert dedup_key(_paper(doi="", title="  A  Title ")) == "title:a title"


def test_save_paper_dedupes_and_keeps_id(conn):
    id1 = save_paper(conn, _paper())
    id2 = save_paper(conn, _paper())  # 同 DOI，不重复插入
    assert id1 == id2
    count = conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()["c"]
    assert count == 1
    row = conn.execute("SELECT keywords FROM papers WHERE id = ?", (id1,)).fetchone()
    assert row["keywords"] == '["kw1", "kw2"]'


def test_get_seen_keys(conn):
    pid1 = save_paper(conn, _paper(doi="10.1/a"))
    pid2 = save_paper(conn, _paper(doi="", title="No DOI Paper"))
    save_recommendation(conn, "a@x.com", pid1, "Must Read", 9, "2026-07-22")
    save_recommendation(conn, "a@x.com", pid2, "Reference", 0, "2026-07-22")
    assert get_seen_keys(conn, "a@x.com") == {"doi:10.1/a", "title:no doi paper"}
    # 未推荐过的论文（仅入库）不算已见
    save_paper(conn, _paper(doi="10.1/unsent"))
    assert "doi:10.1/unsent" not in get_seen_keys(conn, "a@x.com")


def test_seen_keys_are_isolated_between_users(conn):
    pid = save_paper(conn, _paper())
    save_recommendation(conn, "a@x.com", pid, "Must Read", 9, "2026-07-22")
    assert get_seen_keys(conn, "a@x.com") == {"doi:10.1/x"}
    assert get_seen_keys(conn, "b@x.com") == set()  # A 收过不影响 B


def test_save_recommendation_is_idempotent(conn):
    pid = save_paper(conn, _paper())
    save_recommendation(conn, "a@x.com", pid, "Must Read", 9, "2026-07-22")
    save_recommendation(conn, "a@x.com", pid, "Must Read", 9, "2026-07-22")
    count = conn.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"]
    assert count == 1


def test_get_recommendation_paper_ids_display_order(conn):
    """按邮件展示顺序返回：分数降序，同分按写入顺序（id 升序）。"""
    p1 = save_paper(conn, _paper(doi="10.1/a"))
    p2 = save_paper(conn, _paper(doi="10.1/b"))
    p3 = save_paper(conn, _paper(doi="10.1/c"))
    # 写入顺序即展示顺序：p1(60) → p2(70) → p3(70)
    save_recommendation(conn, "a@x.com", p1, "Reference", 60, "2026-07-28")
    save_recommendation(conn, "a@x.com", p2, "Important", 70, "2026-07-28")
    save_recommendation(conn, "a@x.com", p3, "Important", 70, "2026-07-28")
    # 排序重算后应与展示顺序一致：70 分的 p2 在前，同分的 p3 按写入序随后
    assert get_recommendation_paper_ids(conn, "a@x.com", "2026-07-28") == [p2, p3, p1]
    # 其他日期/其他用户互不影响
    assert get_recommendation_paper_ids(conn, "a@x.com", "2026-07-29") == []
    assert get_recommendation_paper_ids(conn, "b@x.com", "2026-07-28") == []


def test_save_analysis_roundtrip(conn):
    pid = save_paper(conn, _paper())
    save_analysis(conn, pid, {"problem": "P", "solution": "S", "finding": "F",
                              "methods": ["scRNA-seq"], "organisms": ["Apis"]})
    save_analysis(conn, pid, {"problem": "P2", "solution": "", "finding": "",
                              "methods": [], "organisms": []})  # REPLACE 覆盖
    rows = conn.execute("SELECT * FROM paper_analysis WHERE paper_id = ?", (pid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["problem"] == "P2"
    assert rows[0]["methods"] == "[]"


def test_save_news_summary(conn):
    pid = save_paper(conn, _paper())
    save_news_summary(conn, pid, "一句话摘要。")
    row = conn.execute(
        "SELECT summary, created_time FROM paper_news_summary WHERE paper_id = ?", (pid,)
    ).fetchone()
    assert row["summary"] == "一句话摘要。"
    assert row["created_time"]


def test_get_paper_id(conn):
    assert get_paper_id(conn, "doi:10.1/x") is None
    pid = save_paper(conn, _paper())
    assert get_paper_id(conn, "doi:10.1/x") == pid


def test_get_analysis_roundtrip(conn):
    pid = save_paper(conn, _paper())
    assert get_analysis(conn, pid) is None
    save_analysis(conn, pid, {"problem": "P", "solution": "S", "finding": "F",
                              "methods": ["scRNA-seq"], "organisms": ["Apis"]})
    assert get_analysis(conn, pid) == {
        "field": "", "problem": "P", "solution": "S", "finding": "F",
        "methods": ["scRNA-seq"], "organisms": ["Apis"],
    }


def test_get_news_summary_roundtrip(conn):
    pid = save_paper(conn, _paper())
    assert get_news_summary(conn, pid) is None
    save_news_summary(conn, pid, "一句话摘要。")
    assert get_news_summary(conn, pid) == "一句话摘要。"


def test_translation_roundtrip_and_replace(conn):
    pid = save_paper(conn, _paper())
    assert get_translation(conn, pid) is None
    t1 = {"title_zh": "题", "background": "背", "methods": "方",
          "results": "结", "significance": "义"}
    save_translation(conn, pid, t1)
    assert get_translation(conn, pid) == t1
    t2 = {"title_zh": "题2", "background": "背2", "methods": "方2",
          "results": "结2", "significance": "义2"}
    save_translation(conn, pid, t2)  # INSERT OR REPLACE 覆盖
    assert get_translation(conn, pid) == t2
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM paper_translation WHERE paper_id = ?", (pid,)
    ).fetchone()["c"]
    assert count == 1


def test_translation_table_migration_adds_columns(tmp_path):
    # 旧形库（仅 title_zh/abstract_zh）重连后应补齐四段列，旧 abstract_zh 列保留
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute("""CREATE TABLE paper_translation (
                       paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
                       title_zh TEXT, abstract_zh TEXT, created_time TEXT NOT NULL)""")
    raw.execute("INSERT INTO paper_translation VALUES (1, '题', '旧摘', '2026-07-01')")
    raw.commit()
    raw.close()

    c = connect(db)
    row = c.execute("SELECT * FROM paper_translation WHERE paper_id = 1").fetchone()
    assert row["abstract_zh"] == "旧摘"      # 旧列留着不管
    assert row["background"] is None         # 新列已补齐
    t = {"title_zh": "题2", "background": "背", "methods": "方",
         "results": "结", "significance": "义"}
    save_translation(c, 1, t)                # 新接口在旧库上可读写
    assert get_translation(c, 1) == t
    c.close()
