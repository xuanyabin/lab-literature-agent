"""规则粗筛打分（三层漏斗的第 2 层）：零成本等权关键词匹配 + tie-break。

对检索层返回的候选论文按用户配置打分并排序，把零相关论文挡在
昂贵的 LLM 处理（分析/新闻/翻译）之外；所有检索词等权（原词或其
aliases/自动扩展变体命中即计一次），同分候选用"标题命中 / 命中频次"
拉开区分度；命中 exclude 词的论文直接剔除。期刊影响力不在本层参与，
由精排（recommendation/ranker.py）journal 维度独立承担；重要性定级
也在精排按 Final Score 绝对阈值判定（宁缺毋滥）。Phase 5 起额外叠加
反馈学习词表命中加分（按有效权重计、单篇封顶，与用户手配词表分离）。
V5 关键词分层（见 config/lab.yaml）：lab_recall（global_core + 用户订阅
topic_groups）参与本层等权打分；lab_topics 里的 rank_only 高噪音词不在
此打分（仅作精排 lab 维度接口），残留 lab_topics 权重会被守卫跳过；
noise_terms 医学噪音词命中按 noise_penalty 软惩罚减分（允许负分沉底，
不淘汰）。
"""

import logging
import re
from pathlib import Path

import yaml

from feedback.vocab import DEFAULT_LEARNED_CONFIG
from sources.paper import Paper

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SCORING_CONFIG = BASE_DIR / "config" / "scoring.yaml"
DEFAULT_JOURNALS_CONFIG = BASE_DIR / "config" / "journals.yaml"

_DEFAULT_WEIGHTS = {
    "species": 1, "methods": 1, "research_interest": 1,
    "keywords": 1,
    # lab_recall（global_core + 订阅 topic_groups，V5）参与等权打分；
    # lab_topics 旧键不直接打分（守卫跳过），其 rank_only 部分只供精排 lab 维度
    "lab_recall": 1,
}

CATEGORY_MUST_READ = "Must Read"
CATEGORY_IMPORTANT = "Important"
CATEGORY_REFERENCE = "Reference"


def load_scoring_config(path: Path = DEFAULT_SCORING_CONFIG,
                        journals_path: Path = DEFAULT_JOURNALS_CONFIG) -> dict:
    """读取打分配置（config/scoring.yaml）并并入期刊分层名单，缺省字段回退默认值。"""
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {
        "weights": {**_DEFAULT_WEIGHTS, **(cfg.get("weights") or {})},
        "title_bonus": cfg.get("title_bonus", 1),
        "frequency_bonus": cfg.get("frequency_bonus", 1),
        "frequency_cap": cfg.get("frequency_cap", 3),
        "journal_tiers": load_journal_tiers(journals_path),
        "journal_channel": cfg.get("journal_channel") or {},
        "learned_score_cap": (cfg.get("learned") or {}).get(
            "score_cap", DEFAULT_LEARNED_CONFIG["score_cap"]),
        "noise_penalty": cfg.get("noise_penalty", 2),
        "personal_fallback": cfg.get("personal_fallback") or {},
    }


def load_journal_tiers(path: Path = DEFAULT_JOURNALS_CONFIG) -> dict[str, str]:
    """读取期刊分层名单（config/journals.yaml），返回 {规范化刊名: "t0"|"t1"}；文件缺失时返回空表。"""
    p = Path(path)
    if not p.exists():
        logger.warning("期刊分层配置不存在：%s，精排期刊维度按未分层处理", p)
        return {}
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {
        _normalize_journal(name): tier
        for tier in ("t0", "t1")
        for name in cfg.get(tier) or []
    }


def _normalize_journal(name: str) -> str:
    """刊名规范化：去括号附加说明（如 "Science (New York, N.Y.)"）、忽略标点与大小写。"""
    name = re.sub(r"\([^)]*\)", " ", name.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name)).strip()


def _text_of(paper: Paper) -> str:
    return " ".join([paper.title, paper.abstract, " ".join(paper.keywords)]).lower()


def score_paper(paper: Paper, user: dict, config: dict) -> int:
    """等权关键词打分 + tie-break（标题命中、命中频次）。

    每个检索词命中即计该类别权重一次（默认全部等权 1 分）：原词的任一
    变体（原词本身或其 aliases）命中即算，同一原词的多个变体命中不重复
    计分。tie-break：变体命中标题加 title_bonus；命中频次按命中次数最多
    的变体计，每多一次加 frequency_bonus（封顶 frequency_cap）。
    """
    text = _text_of(paper)
    title = paper.title.lower()
    aliases = user.get("aliases") or {}
    score = 0
    for field, weight in config["weights"].items():
        if field == "lab_topics":
            continue  # 共享词表不参与个人粗筛（批 13），即使配置残留该权重也跳过
        for term in user.get(field) or []:
            term = term.strip()
            if not term:
                continue
            variants = [v.strip().lower() for v in [term, *(aliases.get(term) or [])] if v and v.strip()]
            hits = max((text.count(v) for v in variants), default=0)
            if not hits:
                continue
            score += weight
            if any(v in title for v in variants):
                score += config.get("title_bonus", 1)
            score += min(hits - 1, config.get("frequency_cap", 3)) * config.get("frequency_bonus", 1)
    # 反馈学习词表（Phase 5）：命中按有效权重加分，单篇封顶 learned_score_cap
    learned = 0
    for term, term_weight in user.get("learned_terms") or []:
        variant = str(term).strip().lower()
        if not variant or variant not in text:
            continue
        learned += max(1, round(term_weight))
        if variant in title:
            learned += config.get("title_bonus", 1)
    score += min(learned, config.get("learned_score_cap", DEFAULT_LEARNED_CONFIG["score_cap"]))
    # 医学噪音软惩罚（V5）：每命中一个 noise_terms 词减 noise_penalty 分，允许负分沉底，不淘汰
    noise = [t.strip().lower() for t in user.get("noise_terms") or [] if t and t.strip()]
    if noise:
        score -= config.get("noise_penalty", 2) * sum(1 for t in noise if t in text)
    return score


def rank_papers(papers: list[Paper], user: dict, config: dict) -> list[tuple[int, Paper]]:
    """返回按分数降序的 [(score, paper)]；命中 exclude 词的论文直接剔除。

    排序稳定：同分论文保持检索返回的原始顺序。
    """
    exclude = [t.strip().lower() for t in user.get("exclude") or [] if t and t.strip()]
    scored = []
    dropped = 0
    for p in papers:
        if exclude and any(t in _text_of(p) for t in exclude):
            dropped += 1
            continue
        scored.append((score_paper(p, user, config), p))
    scored.sort(key=lambda x: -x[0])
    if dropped:
        logger.info("规则粗筛：剔除 %d 篇命中排除词的论文", dropped)
    zero = sum(1 for s, _ in scored if s == 0)
    if zero:
        logger.info("规则粗筛：%d/%d 篇得分为 0（将排在末尾）", zero, len(scored))
    return scored
