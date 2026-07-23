"""反馈学习闭环（Phase 5 核心）：把用户标注转化为学习词表更新，全自动无人工审核。

规则（spec 见 PROJECT.md Phase 5）：
- 高分标注（relevant / save）→ LLM 从摘要提炼新词 → 同一新词在 ≥promote_support
  篇高分论文中出现才提权（防漂移）；已有学习词被文本命中也累计支持并提权；
- 低分标注（not_relevant）→ 只对命中的学习词降权（乘 negative_factor），
  不触碰用户手配词表，也不写 exclude（避免单次误标误杀整个方向）；
- 已读标注（already_read）只记录，不参与学习；
- 所有词表变更写审计日志 logs/feedback_learning.log（JSON Lines），透明可回滚。
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from database.db import (
    get_learned_term, get_unprocessed_feedback, load_learned_terms,
    mark_feedback_processed, upsert_learned_term,
)
from processing.llm import load_prompt

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_LOG = BASE_DIR / "logs" / "feedback_learning.log"

POSITIVE_VALUES = {"relevant", "save"}
NEGATIVE_VALUES = {"not_relevant"}

_TERM_FIELDS = ("research_interest", "keywords", "methods", "species")


def _paper_text(row) -> str:
    return f"{row['title']} {row['abstract'] or ''}".lower()


def _known_terms(user: dict, conn, user_email: str) -> list[str]:
    """提词时禁止重复的词：用户手配词表 + 已提权（weight>0）的学习词。
    候选词（weight=0）允许被再次提炼——同词在 ≥promote_support 篇高分论文中
    出现才提权，二次提炼正是候选词累计支持的途径之一。"""
    manual = [t for f in _TERM_FIELDS for t in user.get(f) or [] if t and t.strip()]
    promoted = [r["term"] for r in load_learned_terms(conn, user_email) if r["weight"] > 0]
    return manual + promoted


def extract_terms(title: str, abstract: str, known: list[str], llm) -> list[str]:
    """LLM 从高分论文提炼新检索词；输出异常时返回空列表（学习跳过，不影响流水线）。"""
    prompt = load_prompt("feedback_term_extraction").safe_substitute(
        known_terms=", ".join(known) or "（无）",
        title=title,
        abstract=abstract or "（无摘要）",
    )
    raw = llm.complete(prompt).strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("提词输出不是合法 JSON，跳过：%.200s", raw)
        return []
    if not isinstance(data, list):
        return []
    known_lower = {k.lower() for k in known}
    seen = set()
    terms = []
    for item in data:
        term = str(item).strip().lower()
        if term and term not in known_lower and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms[:5]


def _audit(action: str, user_email: str, term: str, paper_id: int,
           weight: float, support: int, feedback_value: str) -> None:
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "user": user_email,
        "action": action,
        "term": term,
        "paper_id": paper_id,
        "weight": weight,
        "support": support,
        "feedback": feedback_value,
    }
    AUDIT_LOG.parent.mkdir(exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _reinforce(conn, user_email: str, term: str, paper_id: int, value: str,
               cfg: dict, today: str) -> None:
    """累计支持数并提权：候选（weight=0）→ 达到 promote_support 时提权为 initial_weight，
    之后每篇支持论文再加 boost（封顶 max_weight）。"""
    row = get_learned_term(conn, user_email, term)
    support = (row["support"] if row else 0) + 1
    weight = row["weight"] if row else 0.0
    if support >= cfg["promote_support"]:
        if weight <= 0:
            action = "promote"
            weight = cfg["initial_weight"]
        else:
            action = "boost"
            weight = min(cfg["max_weight"], weight + cfg["boost"])
    else:
        action = "candidate"
    upsert_learned_term(conn, user_email, term, weight, support, today)
    _audit(action, user_email, term, paper_id, weight, support, value)


def _learn_positive(conn, user: dict, row, cfg: dict, llm, today: str) -> None:
    user_email, paper_id, value = row["user_email"], row["paper_id"], row["value"]
    text = _paper_text(row)

    # 已有学习词被论文文本命中：直接累计支持并提权（无需 LLM）
    existing = [r["term"] for r in conn.execute(
        "SELECT term FROM learned_terms WHERE user_email = ?", (user_email,)).fetchall()]
    matched = {t for t in existing if t in text}
    for term in sorted(matched):
        _reinforce(conn, user_email, term, paper_id, value, cfg, today)

    # LLM 提炼新词（与已有词表去重）
    known = _known_terms(user, conn, user_email)
    for term in extract_terms(row["title"], row["abstract"] or "", known, llm):
        if term not in matched:
            _reinforce(conn, user_email, term, paper_id, value, cfg, today)


def _learn_negative(conn, row, cfg: dict) -> None:
    """低分标注：只对论文文本命中的学习词降权，不写 exclude、不碰手配词表。"""
    user_email, paper_id, value = row["user_email"], row["paper_id"], row["value"]
    text = _paper_text(row)
    rows = conn.execute(
        "SELECT term, weight, support, last_seen FROM learned_terms WHERE user_email = ?",
        (user_email,)).fetchall()
    for r in rows:
        if r["weight"] > 0 and r["term"] in text:
            weight = round(r["weight"] * cfg["negative_factor"], 4)
            upsert_learned_term(conn, user_email, r["term"], weight, r["support"], r["last_seen"])
            _audit("downweight", user_email, r["term"], paper_id, weight, r["support"], value)


def learn_from_feedback(conn, user: dict, llm, cfg: dict,
                        today: date | None = None) -> dict:
    """处理该用户全部未学习反馈，返回统计 {"positive": n, "negative": n, "skipped": n}。"""
    today_str = (today or date.today()).isoformat()
    rows = get_unprocessed_feedback(conn, user["email"])
    stats = {"positive": 0, "negative": 0, "skipped": 0}
    done = []
    for row in rows:
        if row["value"] in POSITIVE_VALUES:
            _learn_positive(conn, user, row, cfg, llm, today_str)
            stats["positive"] += 1
        elif row["value"] in NEGATIVE_VALUES:
            _learn_negative(conn, row, cfg)
            stats["negative"] += 1
        else:
            stats["skipped"] += 1
        done.append(row["id"])
    if done:
        mark_feedback_processed(conn, done)
    return stats
