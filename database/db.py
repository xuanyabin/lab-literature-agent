"""SQLite 持久化层（Phase 5）。

数据库文件：项目根目录 literature_agent.db（已被 .gitignore 排除）。
papers / paper_analysis / paper_news_summary / paper_translation 为全局共享表
（同一篇论文只存一份）；recommendations 记录"哪篇论文发给了哪个用户"，跨天去重
按用户隔离（A 收过的论文不影响 B）；feedback 记录用户回传的标注，
learned_terms 是反馈闭环自动维护的学习词表（与用户手配词表分离）。
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
CREATE TABLE IF NOT EXISTS paper_translation (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
    title_zh TEXT,
    background TEXT,
    methods TEXT,
    results TEXT,
    significance TEXT,
    created_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    sent_date TEXT NOT NULL,
    category TEXT,
    score INTEGER,
    UNIQUE(user_email, paper_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    value TEXT NOT NULL,
    reason TEXT,
    created_time TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_email, paper_id, value)
);
CREATE TABLE IF NOT EXISTS learned_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    term TEXT NOT NULL,
    weight REAL NOT NULL,
    support INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL,
    created_time TEXT NOT NULL,
    UNIQUE(user_email, term)
);
"""


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_translation_table(conn)
    return conn


# paper_translation 应有的翻译列（四段结构化摘要，B6 起替代旧 abstract_zh）
_TRANSLATION_COLUMNS = ("title_zh", "background", "methods", "results", "significance")


def _migrate_translation_table(conn: sqlite3.Connection) -> None:
    """旧库已存在早期 paper_translation（仅 title_zh/abstract_zh）时，缺列用
    ALTER TABLE 补齐；旧 abstract_zh 列保留不管。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(paper_translation)")}
    for name in _TRANSLATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE paper_translation ADD COLUMN {name} TEXT")
    conn.commit()


def dedup_key(paper: Paper) -> str:
    """与 sources.pubmed.dedupe 同一规则：DOI 优先，否则规范化标题。"""
    if paper.doi:
        return f"doi:{paper.doi.lower()}"
    return "title:" + " ".join(paper.title.lower().split())


def get_paper_id(conn: sqlite3.Connection, key: str) -> int | None:
    """按 dedup_key 查 papers.id；未入库返回 None。"""
    row = conn.execute("SELECT id FROM papers WHERE dedup_key = ?", (key,)).fetchone()
    return row["id"] if row else None


def get_seen_keys(conn: sqlite3.Connection, user_email: str) -> set[str]:
    """该用户已收到过的论文 dedup_key，用于跨天去重（用户之间互不影响）。"""
    rows = conn.execute(
        """SELECT p.dedup_key FROM recommendations r
           JOIN papers p ON p.id = r.paper_id
           WHERE r.user_email = ?""",
        (user_email,),
    ).fetchall()
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


def get_analysis(conn: sqlite3.Connection, paper_id: int) -> dict | None:
    """读取已缓存的 AI 分析；无行返回 None。键与 analyzer.EMPTY_ANALYSIS 对齐
    （field 列不入库，固定给 ""），methods/organisms 从 JSON 还原。"""
    row = conn.execute(
        "SELECT * FROM paper_analysis WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "field": "",
        "problem": row["problem"] or "",
        "solution": row["solution"] or "",
        "finding": row["finding"] or "",
        "methods": json.loads(row["methods"] or "[]"),
        "organisms": json.loads(row["organisms"] or "[]"),
    }


def get_news_summary(conn: sqlite3.Connection, paper_id: int) -> str | None:
    """读取已缓存的一句话新闻摘要；无行返回 None。"""
    row = conn.execute(
        "SELECT summary FROM paper_news_summary WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    return row["summary"] if row else None


def save_translation(conn: sqlite3.Connection, paper_id: int, translation: dict) -> None:
    """写入中文翻译与四段摘要 {"title_zh", "background", "methods",
    "results", "significance"}（INSERT OR REPLACE 幂等覆盖）。"""
    conn.execute(
        """INSERT OR REPLACE INTO paper_translation
           (paper_id, title_zh, background, methods, results, significance, created_time)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            paper_id,
            translation.get("title_zh", ""),
            translation.get("background", ""),
            translation.get("methods", ""),
            translation.get("results", ""),
            translation.get("significance", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def get_translation(conn: sqlite3.Connection, paper_id: int) -> dict | None:
    """读取已缓存的中文翻译与四段摘要（五字段 dict）；无行返回 None。"""
    row = conn.execute(
        """SELECT title_zh, background, methods, results, significance
           FROM paper_translation WHERE paper_id = ?""",
        (paper_id,),
    ).fetchone()
    if not row:
        return None
    return {key: row[key] or "" for key in _TRANSLATION_COLUMNS}


def save_recommendation(conn: sqlite3.Connection, user_email: str, paper_id: int,
                        category: str, score: int, sent_date: str) -> None:
    """记录"论文已发给某用户"（幂等：同一用户同一论文只记一次）。"""
    conn.execute(
        """INSERT OR IGNORE INTO recommendations
           (user_email, paper_id, sent_date, category, score)
           VALUES (?, ?, ?, ?, ?)""",
        (user_email, paper_id, sent_date, category, score),
    )
    conn.commit()


def get_week_recommendations(conn: sqlite3.Connection, user_email: str,
                             since_date: str) -> list[sqlite3.Row]:
    """该用户 since_date 以来收到过的推荐（含一句话新闻），供周报聚合。

    排序：Must Read → Important → Reference，同级按分数降序。
    """
    return conn.execute(
        """SELECT r.category, r.score, r.sent_date,
                  p.id AS paper_id, p.title, p.journal, p.date, p.url, p.keywords,
                  s.summary AS news
           FROM recommendations r
           JOIN papers p ON p.id = r.paper_id
           LEFT JOIN paper_news_summary s ON s.paper_id = p.id
           WHERE r.user_email = ? AND r.sent_date >= ?
           ORDER BY CASE r.category
                        WHEN 'Must Read' THEN 0
                        WHEN 'Important' THEN 1
                        ELSE 2
                    END, r.score DESC""",
        (user_email, since_date),
    ).fetchall()


def save_feedback(conn: sqlite3.Connection, user_email: str, paper_id: int,
                  value: str, reason: str = "") -> bool:
    """记录一条用户标注（幂等：同一用户对同一论文的同一标注只记一次），返回是否为新插入。"""
    cur = conn.execute(
        """INSERT OR IGNORE INTO feedback
           (user_email, paper_id, value, reason, created_time)
           VALUES (?, ?, ?, ?, ?)""",
        (user_email, paper_id, value, reason, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.rowcount > 0


def get_unprocessed_feedback(conn: sqlite3.Connection, user_email: str) -> list[sqlite3.Row]:
    """该用户尚未进入学习闭环的反馈，附带论文标题与摘要（按时间升序）。"""
    return conn.execute(
        """SELECT f.id, f.user_email, f.paper_id, f.value, f.reason, p.title, p.abstract
           FROM feedback f JOIN papers p ON p.id = f.paper_id
           WHERE f.user_email = ? AND f.processed = 0
           ORDER BY f.id""",
        (user_email,),
    ).fetchall()


def mark_feedback_processed(conn: sqlite3.Connection, feedback_ids: list[int]) -> None:
    conn.executemany("UPDATE feedback SET processed = 1 WHERE id = ?",
                     [(i,) for i in feedback_ids])
    conn.commit()


def get_learned_term(conn: sqlite3.Connection, user_email: str, term: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM learned_terms WHERE user_email = ? AND term = ?",
        (user_email, term),
    ).fetchone()


def upsert_learned_term(conn: sqlite3.Connection, user_email: str, term: str,
                        weight: float, support: int, last_seen: str) -> None:
    """写入/更新一个学习词（weight 为未衰减的原始权重，衰减在读取时计算）。"""
    conn.execute(
        """INSERT INTO learned_terms (user_email, term, weight, support, last_seen, created_time)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_email, term)
           DO UPDATE SET weight = excluded.weight, support = excluded.support,
                         last_seen = excluded.last_seen""",
        (user_email, term, weight, support, last_seen,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def load_learned_terms(conn: sqlite3.Connection, user_email: str) -> list[sqlite3.Row]:
    """该用户全部学习词（含未提权的候选词与已衰减词，过滤在读取方做）。"""
    return conn.execute(
        "SELECT term, weight, support, last_seen FROM learned_terms WHERE user_email = ?",
        (user_email,),
    ).fetchall()
