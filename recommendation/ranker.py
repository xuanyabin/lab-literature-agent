"""个性化精排（三层漏斗的第 3 层）：六维加权 Final Score + AI 推荐理由。

规则粗筛（recommendation/scorer.py）选出当日候选后，本模块对每篇论文
计算 0-100 的 Final Score（权重见 config/scoring.yaml 的 ranker 节）：

    personal  个人相关度（LLM 依据个人研究画像语义判断）
    lab       实验室方向相关度（lab_topics 命中数，规则）
    journal   期刊影响力（journals.yaml 分层，规则；期刊因素只在精排体现）
    novelty   新颖性（LLM 依据 AI 分析判断）
    method    方法相关度（个人 methods 命中数，规则）
    recency   时效性（发表日期距今，规则）

同时生成一句中文推荐理由展示在邮件论文卡片上；LLM 输出异常时
个人相关度/新颖性回退中性分 50，保证流水线不中断（日预算耗尽除外：
BudgetExhaustedError 直接向上传播，快速失败不发空壳邮件）。重要性按 Final Score
绝对阈值定级（ranker.thresholds，宁缺毋滥：当日全部低分则可以没有
Must Read）；不再按固定配额凑数。
"""

import json
import logging
from datetime import date
from pathlib import Path

import yaml

from processing.llm import BudgetExhaustedError, load_prompt
from recommendation.scorer import (
    CATEGORY_IMPORTANT,
    CATEGORY_MUST_READ,
    CATEGORY_REFERENCE,
    _normalize_journal,
    _text_of,
)
from sources.paper import Paper

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SCORING_CONFIG = BASE_DIR / "config" / "scoring.yaml"

DEFAULT_RANKER_WEIGHTS = {
    "personal": 20, "lab": 20, "journal": 30,
    "novelty": 10, "method": 10, "recency": 10,
}
DEFAULT_RANKER_THRESHOLDS = {"must_read": 75, "important": 60}

_NEUTRAL_JUDGMENT = {"personal_relevance": 50, "novelty": 50, "reason": ""}


def load_ranker_weights(path: Path = DEFAULT_SCORING_CONFIG) -> dict:
    """读取 scoring.yaml 的 ranker.weights，文件缺失或字段缺省时回退默认值。"""
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_RANKER_WEIGHTS)
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    weights = (cfg.get("ranker") or {}).get("weights") or {}
    return {**DEFAULT_RANKER_WEIGHTS, **weights}


def load_ranker_thresholds(path: Path = DEFAULT_SCORING_CONFIG) -> dict:
    """读取 scoring.yaml 的 ranker.thresholds（Final Score 绝对定级阈值），缺省回退默认值。"""
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_RANKER_THRESHOLDS)
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    thresholds = (cfg.get("ranker") or {}).get("thresholds") or {}
    return {**DEFAULT_RANKER_THRESHOLDS, **thresholds}


def _category_of(score: int, thresholds: dict) -> str:
    """按绝对阈值定级：≥ must_read → Must Read；≥ important → Important；其余 Reference。"""
    if score >= thresholds.get("must_read", DEFAULT_RANKER_THRESHOLDS["must_read"]):
        return CATEGORY_MUST_READ
    if score >= thresholds.get("important", DEFAULT_RANKER_THRESHOLDS["important"]):
        return CATEGORY_IMPORTANT
    return CATEGORY_REFERENCE


def _hits(text: str, terms: list, aliases: dict) -> int:
    """命中的检索词个数（同一词的多个变体只算一次）。"""
    n = 0
    for term in terms or []:
        term = term.strip()
        if not term:
            continue
        variants = [v.strip().lower() for v in [term, *(aliases.get(term) or [])] if v and v.strip()]
        if any(v in text for v in variants):
            n += 1
    return n


def lab_relevance(paper: Paper, user: dict) -> int:
    """实验室方向相关度：lab_topics 命中 0/1/≥2 个 → 0/50/100。"""
    return min(_hits(_text_of(paper), user.get("lab_topics"), user.get("aliases") or {}), 2) * 50


def method_relevance(paper: Paper, user: dict) -> int:
    """方法相关度：个人 methods 命中 0/1/≥2 个 → 0/50/100。"""
    return min(_hits(_text_of(paper), user.get("methods"), user.get("aliases") or {}), 2) * 50


def journal_influence(paper: Paper, journal_tiers: dict) -> int:
    """期刊影响力：T0 → 100，T1 → 70，未分层 → 30。"""
    tier = (journal_tiers or {}).get(_normalize_journal(paper.journal))
    return {"t0": 100, "t1": 70}.get(tier, 30)


def recency(paper: Paper, today: date | None = None) -> int:
    """时效性：当天/1天 → 100，2天 → 80，3天 → 60，一周内 → 40，更早 → 20；日期无法解析 → 50。"""
    today = today or date.today()
    try:
        published = date.fromisoformat((paper.date or "")[:10])
    except ValueError:
        return 50
    days = (today - published).days
    if days <= 1:
        return 100
    if days == 2:
        return 80
    if days == 3:
        return 60
    if days <= 7:
        return 40
    return 20


def final_score(dims: dict, weights: dict) -> int:
    """六维（各 0-100）按权重（合计 100）加权，返回 0-100 整数。"""
    total = sum(weights.values()) or 100
    return round(sum(dims[k] * weights.get(k, 0) for k in dims) / total)


def judge_paper(paper: Paper, analysis: dict, user: dict, llm) -> dict:
    """LLM 语义判断：{personal_relevance, novelty, reason}；输出异常回退中性分。"""
    prompt = load_prompt("recommendation_reason").safe_substitute(
        interests=", ".join(user.get("research_interest") or []) or "（无）",
        keywords=", ".join(user.get("keywords") or []) or "（无）",
        species=", ".join(user.get("species") or []) or "（无）",
        methods=", ".join(user.get("methods") or []) or "（无）",
        title=paper.title,
        abstract=paper.abstract or "（无摘要）",
        problem=analysis.get("problem", ""),
        finding=analysis.get("finding", ""),
    )
    return _parse_judgment(llm.complete(prompt))


def _parse_judgment(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        data = json.loads(text)
        return {
            "personal_relevance": max(0, min(100, int(data["personal_relevance"]))),
            "novelty": max(0, min(100, int(data["novelty"]))),
            "reason": str(data.get("reason", "")).strip(),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("精排输出不是合法 JSON，回退中性分：%.100s", raw)
        return dict(_NEUTRAL_JUDGMENT)


def rank_items(items: list[dict], user: dict, llm, journal_tiers: dict,
               weights: dict, thresholds: dict, today: date | None = None) -> list[dict]:
    """对 AI 处理完的 items 计算 Final Score，按分数降序重排并按绝对阈值定级。

    每个 item 增加 "score"（0-100）、"reason"（推荐理由）、"category" 字段。
    定级宁缺毋滥：当日全部低分则没有 Must Read / Important，不凑配额。
    """
    scored = []
    for it in items:
        paper, analysis = it["paper"], it["analysis"]
        try:
            judgment = judge_paper(paper, analysis, user, llm)
        except BudgetExhaustedError:
            raise  # 日预算耗尽：快速失败，不发空壳邮件
        except Exception:
            logger.warning("精排 LLM 判断失败，回退中性分：%s", paper.title[:60], exc_info=True)
            judgment = dict(_NEUTRAL_JUDGMENT)
        dims = {
            "personal": judgment["personal_relevance"],
            "lab": lab_relevance(paper, user),
            "journal": journal_influence(paper, journal_tiers),
            "novelty": judgment["novelty"],
            "method": method_relevance(paper, user),
            "recency": recency(paper, today),
        }
        it["score"] = final_score(dims, weights)
        it["reason"] = judgment["reason"]
        scored.append((it["score"], it))
    scored.sort(key=lambda x: -x[0])
    for score, it in scored:
        it["category"] = _category_of(score, thresholds)
    return [it for _, it in scored]
