import pytest

from database.db import (
    connect, dedup_key, get_analysis, get_news_summary, get_paper_id,
    get_seen_keys, get_translation, save_analysis, save_news_summary,
    save_paper, save_recommendation, save_translation,
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
    save_translation(conn, pid, "题", "摘")
    assert get_translation(conn, pid) == {"title_zh": "题", "abstract_zh": "摘"}
    save_translation(conn, pid, "题2", "摘2")  # INSERT OR REPLACE 覆盖
    assert get_translation(conn, pid) == {"title_zh": "题2", "abstract_zh": "摘2"}
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM paper_translation WHERE paper_id = ?", (pid,)
    ).fetchone()["c"]
    assert count == 1
