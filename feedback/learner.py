"""反馈学习闭环（Phase 5 核心）：把用户标注转化为学习词表更新，全自动无人工审核。

待学习反馈来自 feedback_data/pending 文件队列（collector 双写，见 feedback/store.py）；
学后文件归档 processed/YYYY-MM，并把 feedback 表对应行标 processed=1
（db 表保留给周/月报统计，学习队列以文件为准）。

规则（五星标注，B2 起替代旧四值；spec 见 PROJECT.md Phase 5）：
- 正反馈（⭐4 / ⭐5）→ LLM 从摘要提炼新词 → 同一新词在 ≥promote_support
  篇正反馈论文中出现才提权（防漂移）；已有学习词被文本命中也累计支持并提权；
- 中性（⭐3）只记录，不参与学习；
- 弱负反馈（⭐2）→ 只对命中的学习词降权（乘 negative_factor_weak）；
- 强负反馈（⭐1）→ 命中的学习词乘 negative_factor_strong，且同一（用户, 词）
  累计第 2 次强负时在审计日志追加 exclude_candidate 记录（人工排查线索，
  不自动写 exclude，避免单次误标误杀整个方向）；累计次数扫描现有审计日志得到；
- 均不触碰用户手配词表；所有词表变更写审计日志 logs/feedback_learning.log
  （JSON Lines），透明可回滚。
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from database.db import (
    get_learned_term, load_learned_terms, mark_feedback_processed, upsert_learned_term,
)
from feedback import store
from processing.llm import load_prompt

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_LOG = BASE_DIR / "logs" / "feedback_learning.log"

POSITIVE_VALUES = {"4", "5"}
NEUTRAL_VALUES = {"3"}
WEAK_NEGATIVE_VALUES = {"2"}
STRONG_NEGATIVE_VALUES = {"1"}
NEGATIVE_VALUES = WEAK_NEGATIVE_VALUES | STRONG_NEGATIVE_VALUES

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


def _strong_negative_count(user_email: str, term: str) -> int:
    """扫描审计日志，统计同一（用户, 词）的强负（⭐1）降权事件累计次数（含刚写入的本次）。"""
    if not AUDIT_LOG.exists():
        return 0
    count = 0
    with AUDIT_LOG.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 容忍损坏行，不中断学习闭环
            if rec.get("action") == "downweight" and rec.get("user") == user_email \
                    and rec.get("term") == term \
                    and rec.get("feedback") in STRONG_NEGATIVE_VALUES:
                count += 1
    return count


def _learn_negative(conn, row, cfg: dict) -> None:
    """负反馈：弱负（⭐2）命中的学习词 ×negative_factor_weak，强负（⭐1）×negative_factor_strong；
    同一（用户, 词）强负累计第 2 次时追加 exclude_candidate 审计记录。不写 exclude、不碰手配词表。"""
    user_email, paper_id, value = row["user_email"], row["paper_id"], row["value"]
    factor = cfg["negative_factor_strong"] if value in STRONG_NEGATIVE_VALUES \
        else cfg["negative_factor_weak"]
    text = _paper_text(row)
    rows = conn.execute(
        "SELECT term, weight, support, last_seen FROM learned_terms WHERE user_email = ?",
        (user_email,)).fetchall()
    for r in rows:
        if r["weight"] > 0 and r["term"] in text:
            weight = round(r["weight"] * factor, 4)
            upsert_learned_term(conn, user_email, r["term"], weight, r["support"], r["last_seen"])
            _audit("downweight", user_email, r["term"], paper_id, weight, r["support"], value)
            if value in STRONG_NEGATIVE_VALUES \
                    and _strong_negative_count(user_email, r["term"]) == 2:
                _audit("exclude_candidate", user_email, r["term"], paper_id,
                       weight, r["support"], value)


def learn_from_feedback(conn, user: dict, llm, cfg: dict,
                        today: date | None = None,
                        base_dir: Path = store.DEFAULT_BASE_DIR) -> dict:
    """处理该用户在 pending 队列中的全部反馈，返回统计
    {"positive": n, "negative": n, "skipped": n}；学后文件归档 processed/YYYY-MM，
    对应 feedback 表行同步标 processed=1（保持 db 语义一致）。"""
    today_str = (today or date.today()).isoformat()
    entries = [e for e in store.load_pending(base_dir) if e["user_email"] == user["email"]]
    stats = {"positive": 0, "negative": 0, "skipped": 0}
    done_paths = []
    done_ids = []
    for entry in entries:
        paper = conn.execute("SELECT title, abstract FROM papers WHERE id = ?",
                             (entry["paper_id"],)).fetchone()
        if paper is None:
            logger.warning("反馈指向不存在的论文 id=%s，归档跳过", entry["paper_id"])
            stats["skipped"] += 1
        else:
            row = {**entry, "title": paper["title"], "abstract": paper["abstract"]}
            if entry["value"] in POSITIVE_VALUES:
                _learn_positive(conn, user, row, cfg, llm, today_str)
                stats["positive"] += 1
            elif entry["value"] in NEGATIVE_VALUES:
                _learn_negative(conn, row, cfg)
                stats["negative"] += 1
            else:
                stats["skipped"] += 1
        done_paths.append(entry["path"])
        done_ids += [r["id"] for r in conn.execute(
            """SELECT id FROM feedback
               WHERE user_email = ? AND paper_id = ? AND value = ? AND processed = 0""",
            (entry["user_email"], entry["paper_id"], entry["value"])).fetchall()]
    for path in done_paths:
        store.mark_processed(path, base_dir)
    if done_ids:
        mark_feedback_processed(conn, done_ids)
    return stats
