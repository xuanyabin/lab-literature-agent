"""规则粗筛打分（三层漏斗的第 2 层）：零成本加权关键词匹配 + tie-break。

对检索层返回的候选论文按用户配置打分并排序，把零相关论文挡在
昂贵的 LLM 处理（分析/新闻/翻译）之外；同分候选用"标题命中 /
命中频次 / 期刊分层"拉开区分度；assign_categories 再按配额把
排序结果定级为 Must Read / Important / Reference；journal_fallback
在当日强相关不足时用 limit 之外的高水平分层期刊论文递补兜底。
LLM 精排在 Phase 4 接入；Phase 5 起额外叠加反馈学习词表命中加分
（按有效权重计、单篇封顶，与用户手配词表分离）。
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

_DEFAULT_WEIGHTS = {"species": 3, "methods": 2, "research_interest": 1, "keywords": 1}
_DEFAULT_TIERS = {"must_read": 3, "important": 5}

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
        "journal_t0": cfg.get("journal_t0", 5),
        "journal_t1": cfg.get("journal_t1", 2),
        "tiers": {**_DEFAULT_TIERS, **(cfg.get("tiers") or {})},
        "journal_tiers": load_journal_tiers(journals_path),
        "learned_score_cap": (cfg.get("learned") or {}).get(
            "score_cap", DEFAULT_LEARNED_CONFIG["score_cap"]),
    }


def load_journal_tiers(path: Path = DEFAULT_JOURNALS_CONFIG) -> dict[str, str]:
    """读取期刊分层名单（config/journals.yaml），返回 {规范化刊名: "t0"|"t1"}；文件缺失时返回空表。"""
    p = Path(path)
    if not p.exists():
        logger.warning("期刊分层配置不存在：%s，期刊加分跳过", p)
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
    """关键词加权打分 + tie-break（标题命中、命中频次、期刊分层）。

    每个检索词命中即计该类别权重一次：原词的任一变体（原词本身或其
    aliases）命中即算，同一原词的多个变体命中不重复计权重。
    tie-break：变体命中标题加 title_bonus；命中频次按命中次数最多的
    变体计，每多一次加 frequency_bonus（封顶 frequency_cap）；
    期刊命中 journals.yaml 分层名单加 journal_t0 / journal_t1。
    """
    text = _text_of(paper)
    title = paper.title.lower()
    aliases = user.get("aliases") or {}
    score = 0
    for field, weight in config["weights"].items():
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
    tier = (config.get("journal_tiers") or {}).get(_normalize_journal(paper.journal))
    if tier:
        score += config.get(f"journal_{tier}", 0)
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


def journal_fallback(ranked: list[tuple[int, Paper]], config: dict,
                     limit: int) -> list[tuple[int, Paper]]:
    """低相关兜底：top-limit 中强相关不足 must_read 配额时，用分层期刊论文递补。

    强相关 = 分数超过 journal_t0（即高于"零关键词命中、仅靠顶刊加分"的水平）。
    递补来源：limit 之外按序取 T0/T1 分层期刊论文，替换 shortlist 尾部最弱的
    非分层论文，递补数量 = 缺口数，最后按分数重新稳定排序。
    候选总数不超过 limit、强相关足够、或 limit 之外无分层论文时原样返回。
    """
    shortlist = ranked[:limit]
    if len(ranked) <= limit:
        return shortlist
    strong = sum(1 for s, _ in shortlist if s > config.get("journal_t0", 5))
    gap = (config.get("tiers") or {}).get("must_read", 3) - strong
    if gap <= 0:
        return shortlist
    tiers_map = config.get("journal_tiers") or {}

    def tier_of(paper: Paper) -> str:
        return tiers_map.get(_normalize_journal(paper.journal), "")

    result = list(shortlist)
    replaced = 0
    for cand in ((s, p) for s, p in ranked[limit:] if tier_of(p)):
        if replaced >= gap:
            break
        for i in range(len(result) - 1, -1, -1):
            if not tier_of(result[i][1]):
                result[i] = cand
                replaced += 1
                break
    if replaced:
        result.sort(key=lambda x: -x[0])
        logger.info("低相关兜底：top-%d 强相关仅 %d 篇，以 %d 篇分层期刊论文递补",
                    limit, strong, replaced)
    return result


def assign_categories(ranked: list[tuple[int, Paper]], tiers: dict) -> list[tuple[int, str, Paper]]:
    """把按分数降序的 [(score, paper)] 按配额定级为 [(score, category, paper)]。

    前 must_read 篇为 Must Read，接下来 important 篇为 Important，其余 Reference；
    得分为 0 的论文始终为 Reference 且不占 Must Read/Important 配额（宁缺毋滥）。
    """
    must_read_left = tiers.get("must_read", 3)
    important_left = tiers.get("important", 5)
    out = []
    for score, paper in ranked:
        if score > 0 and must_read_left > 0:
            category, must_read_left = CATEGORY_MUST_READ, must_read_left - 1
        elif score > 0 and important_left > 0:
            category, important_left = CATEGORY_IMPORTANT, important_left - 1
        else:
            category = CATEGORY_REFERENCE
        out.append((score, category, paper))
    return out
