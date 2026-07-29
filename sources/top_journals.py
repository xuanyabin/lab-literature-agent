"""顶刊直采通道：绕过关键词召回，按刊名直接抓取 T0/T1 期刊最新论文。

关键词召回（global_pool 的分簇检索）只能捞回标题/摘要命中实验室词表的论文，
顶刊中与实验室方向弱相关但值得关注的论文因此进不了候选池（实测全局池里
Nature/Science/Cell 为 0）。本通道按 config/journals.yaml 的刊名直接用
"<journal>"[jour] 检索 PubMed 最近论文并入全局池；粗筛仍不打期刊分
（V4 原则不变），由各用户精排的 journal 维度与 LLM 语义判断决定是否推送，
推送下限（ranker.thresholds.push_floor）兜底质量。
"""

import logging
import time
from pathlib import Path

import yaml

from sources import pubmed
from sources.paper import Paper

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_JOURNALS_CONFIG = BASE_DIR / "config" / "journals.yaml"


def load_journal_names(path: Path = DEFAULT_JOURNALS_CONFIG,
                       tiers: tuple = ("t0",)) -> list[str]:
    """读取 journals.yaml 中指定分层的原始刊名（供 [jour] 检索用，不做规范化）。"""
    p = Path(path)
    if not p.exists():
        logger.warning("期刊分层配置不存在：%s，顶刊通道无刊可抓", p)
        return []
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [str(name) for tier in tiers for name in cfg.get(tier) or []]


def fetch_top_journals(journal_names: list[str], days: int,
                       retmax_per_journal: int = 20) -> list[Paper]:
    """逐刊检索 PubMed 最近 days 天论文并合并（不去重，全局去重由调用方统一做）。

    单刊异常记日志后继续其余刊；刊间 sleep 0.4s 避免触发 NCBI 限流。
    """
    papers: list[Paper] = []
    for name in journal_names:
        try:
            pmids = pubmed.search_pmids(f'"{name}"[jour]', days, retmax_per_journal)
            if pmids:
                logger.info("顶刊通道：%s 命中 %d 篇", name, len(pmids))
                papers.extend(pubmed.fetch_by_pmids(pmids))
        except Exception:
            logger.warning("顶刊通道：%s 检索失败，跳过该刊", name, exc_info=True)
        time.sleep(0.4)
    return papers
