"""scripts/list_papers.py 的查询逻辑测试（临时库，不碰真实 literature_agent.db）。"""

from datetime import date

import pytest

from database.db import connect, save_paper
from scripts.list_papers import query_papers
from sources.paper import Paper


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def _paper(doi, title, pub_date):
    return Paper(title=title, abstract="abs", authors="Zhang", journal="Cell",
                 date=pub_date, doi=doi, url="http://x", keywords=["kw"])


def test_query_by_seen_date(conn):
    save_paper(conn, _paper("10.1/a", "Paper A", "2026-07-30"))
    save_paper(conn, _paper("10.1/b", "Paper B", "2026-07-29"))
    today = date.today().isoformat()

    rows = query_papers(conn, seen_date=today)
    assert [r["title"] for r in rows] == ["Paper A", "Paper B"]
    assert query_papers(conn, seen_date="1999-01-01") == []


def test_query_by_pub_date(conn):
    save_paper(conn, _paper("10.1/a", "Paper A", "2026-07-30"))
    save_paper(conn, _paper("10.1/b", "Paper B", "2026-07-29"))

    rows = query_papers(conn, pub_date="2026-07-30")
    assert [r["title"] for r in rows] == ["Paper A"]
    assert rows[0]["journal"] == "Cell"


def test_query_by_days(conn):
    save_paper(conn, _paper("10.1/a", "Paper A", "2026-07-30"))
    rows = query_papers(conn, days=3)
    assert len(rows) == 1
    assert rows[0]["seen"] == date.today().isoformat()
