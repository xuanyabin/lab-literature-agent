"""SQLite 持久化层（Phase 2）。

数据库文件：项目根目录 literature_agent.db（已被 .gitignore 排除）。
Phase 2 范围：papers / paper_analysis / paper_news_summary 三张表；
users、scores、feedback 等表在后续 Phase 加入。
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sources.paper import Paper

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "literature_agent.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    abstract TEXT,
    authors TEXT,
    journal TEXT,
    date TEXT,
    doi TEXT,
    url TEXT,
    keywords TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_analysis (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
    problem TEXT,
    solution TEXT,
    finding TEXT,
    methods TEXT,
    organisms TEXT,
    created_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_news_summary (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
    summary TEXT,
    created_time TEXT NOT NULL
);
"""


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def dedup_key(paper: Paper) -> str:
    """与 sources.pubmed.dedupe 同一规则：DOI 优先，否则规范化标题。"""
    if paper.doi:
        return f"doi:{paper.doi.lower()}"
    return "title:" + " ".join(paper.title.lower().split())


def get_seen_keys(conn: sqlite3.Connection) -> set[str]:
    """所有已入库论文的 dedup_key，用于跨天去重（昨天发过的今天不再发）。"""
    rows = conn.execute("SELECT dedup_key FROM papers").fetchall()
    return {row["dedup_key"] for row in rows}


def save_paper(conn: sqlite3.Connection, paper: Paper) -> int:
    """插入论文（已存在则直接返回原 id），返回 papers.id。"""
    key = dedup_key(paper)
    conn.execute(
        """INSERT OR IGNORE INTO papers
           (title, abstract, authors, journal, date, doi, url, keywords, dedup_key, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            paper.title, paper.abstract, paper.authors, paper.journal, paper.date,
            paper.doi, paper.url, json.dumps(paper.keywords, ensure_ascii=False),
            key, datetime.now(timezone.utc).isoformat(),
        ),
    )
    row = conn.execute("SELECT id FROM papers WHERE dedup_key = ?", (key,)).fetchone()
    conn.commit()
    return row["id"]


def save_analysis(conn: sqlite3.Connection, paper_id: int, analysis: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO paper_analysis
           (paper_id, problem, solution, finding, methods, organisms, created_time)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            paper_id,
            analysis.get("problem", ""),
            analysis.get("solution", ""),
            analysis.get("finding", ""),
            json.dumps(analysis.get("methods", []), ensure_ascii=False),
            json.dumps(analysis.get("organisms", []), ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def save_news_summary(conn: sqlite3.Connection, paper_id: int, summary: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO paper_news_summary (paper_id, summary, created_time)
           VALUES (?, ?, ?)""",
        (paper_id, summary, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
