"""规则粗筛打分（三层漏斗的第 2 层）：零成本加权关键词匹配。

对检索层返回的候选论文按用户配置打分并排序，把零相关论文挡在
昂贵的 LLM 处理（分析/新闻/翻译）之外；LLM 精排与完整评分模型在 Phase 4 接入。
"""

import logging
from pathlib import Path

import yaml

from sources.paper import Paper

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SCORING_CONFIG = BASE_DIR / "config" / "scoring.yaml"

_DEFAULT_WEIGHTS = {"species": 3, "methods": 2, "research_interest": 1, "keywords": 1}


def load_scoring_config(path: Path = DEFAULT_SCORING_CONFIG) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {"weights": {**_DEFAULT_WEIGHTS, **(cfg.get("weights") or {})}}


def _text_of(paper: Paper) -> str:
    return " ".join([paper.title, paper.abstract, " ".join(paper.keywords)]).lower()


def score_paper(paper: Paper, user: dict, weights: dict) -> int:
    """每命中一个该类别检索词加对应权重分；同一词重复出现只计一次。

    支持用户 yaml 中的 aliases 字段（{原词: [别名...]}，由 term_expander 生成、
    人工审核后写入）：原词的任一变体（原词本身或其别名）命中即计分一次，
    同一原词的多个变体命中不重复计分。
    """
    text = _text_of(paper)
    aliases = user.get("aliases") or {}
    score = 0
    for field, weight in weights.items():
        for term in user.get(field) or []:
            term = term.strip()
            if not term:
                continue
            variants = [term, *(aliases.get(term) or [])]
            if any(v.strip().lower() in text for v in variants if v and v.strip()):
                score += weight
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
        scored.append((score_paper(p, user, config["weights"]), p))
    scored.sort(key=lambda x: -x[0])
    if dropped:
        logger.info("规则粗筛：剔除 %d 篇命中排除词的论文", dropped)
    zero = sum(1 for s, _ in scored if s == 0)
    if zero:
        logger.info("规则粗筛：%d/%d 篇得分为 0（将排在末尾）", zero, len(scored))
    return scored
