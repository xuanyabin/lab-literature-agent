import pytest

from database.db import (
    connect, dedup_key, get_seen_keys, save_analysis, save_news_summary, save_paper,
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
    save_paper(conn, _paper(doi="10.1/a"))
    save_paper(conn, _paper(doi="", title="No DOI Paper"))
    assert get_seen_keys(conn) == {"doi:10.1/a", "title:no doi paper"}


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
