"""顶刊直采通道：绕过关键词召回，按刊名直接抓取 T0/T1 期刊最新论文。

关键词召回（global_pool 的分簇检索）只能捞回标题/摘要命中实验室词表的论文，
顶刊中与实验室方向弱相关但值得关注的论文因此进不了候选池（实测全局池里
Nature/Science/Cell 为 0）。本通道按 config/journals.yaml 的刊名直接用
"<journal>"[jour] 检索 PubMed 最近论文并入全局池；粗筛仍不打期刊分
（V4 原则不变），由各用户精排的 journal 维度与 LLM 语义判断决定是否推送，
推送下限（ranker.thresholds.push_floor）兜底质量。

两路来源（2026-08-07 起）：
1. PubMed 按刊检索：日常路径用 datetype="edat"（入库日期）开窗，避免大刊
   "pdat 早于索引日"的论文在 pdat 窗口滑过后永久漏召回；
2. Crossref 直采（fetch_crossref_journals）：按 journals.yaml issn 段的
   刊名→ISSN 映射直接查 Crossref works API，出版商注册 DOI 当天即可捕获，
   补齐 PubMed 索引延迟（1~3 天）。DOI 与现有 dedup_key 规则一致，
   同 DOI 的 PubMed 完整版先入池时 PubMed 版本优先（dedupe 保留先出现者）。
"""

import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import yaml

from sources import pubmed
from sources.paper import Paper

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_JOURNALS_CONFIG = BASE_DIR / "config" / "journals.yaml"

CROSSREF_BASE = "https://api.crossref.org"


def load_journal_names(path: Path = DEFAULT_JOURNALS_CONFIG,
                       tiers: tuple = ("t0",)) -> list[str]:
    """读取 journals.yaml 中指定分层的原始刊名（供 [jour] 检索用，不做规范化）。"""
    p = Path(path)
    if not p.exists():
        logger.warning("期刊分层配置不存在：%s，顶刊通道无刊可抓", p)
        return []
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [str(name) for tier in tiers for name in cfg.get(tier) or []]


def load_journal_issns(path: Path = DEFAULT_JOURNALS_CONFIG,
                       tiers: tuple = ("t0",)) -> dict[str, str]:
    """读取 journals.yaml issn 段，返回 {刊名: ISSN}，只保留指定分层内的刊。

    issn 段的键必须与 t0/t1 列表中的刊名原文一致；没有 ISSN 的刊不参与
    Crossref 直采（仍走 PubMed 按刊检索）。
    """
    p = Path(path)
    if not p.exists():
        return {}
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    issns = cfg.get("issn") or {}
    if not isinstance(issns, dict):
        logger.warning("journals.yaml 的 issn 段不是映射，忽略 Crossref 直采")
        return {}
    names = set(load_journal_names(p, tiers=tiers))
    return {str(name): str(issn) for name, issn in issns.items() if name in names}


def fetch_top_journals(journal_names: list[str], days: int,
                       retmax_per_journal: int = 20,
                       datetype: str = "pdat") -> list[Paper]:
    """逐刊检索 PubMed 最近 days 天论文并合并（不去重，全局去重由调用方统一做）。

    datetype 语义同 pubmed.search_pmids；日常路径传 "edat" 防永久漏召回。
    单刊异常记日志后继续其余刊；刊间 sleep 0.4s 避免触发 NCBI 限流。
    """
    papers: list[Paper] = []
    for name in journal_names:
        try:
            pmids = pubmed.search_pmids(f'"{name}"[jour]', days, retmax_per_journal,
                                        datetype=datetype)
            if pmids:
                logger.info("顶刊通道：%s 命中 %d 篇", name, len(pmids))
                papers.extend(pubmed.fetch_by_pmids(pmids))
        except Exception:
            logger.warning("顶刊通道：%s 检索失败，跳过该刊", name, exc_info=True)
        time.sleep(0.4)
    return papers


def _strip_jats(text: str) -> str:
    """Crossref 摘要常带 JATS 标签（<jats:p> 等），剥成纯文本。"""
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())


def _crossref_date(item: dict) -> str:
    """取 published / published-online / issued 的 date-parts 拼 YYYY-MM-DD。"""
    for field in ("published", "published-online", "issued"):
        parts = (item.get(field) or {}).get("date-parts")
        if parts and parts[0] and parts[0][0]:
            y, m, d = (parts[0] + [1, 1])[:3]
            return f"{y:04d}-{m:02d}-{d:02d}"
    return ""


def parse_crossref_items(items: list[dict], journal_fallback: str) -> list[Paper]:
    """把 Crossref works 条目解析为 Paper；缺标题或 DOI 的条目跳过。"""
    papers: list[Paper] = []
    for item in items:
        titles = item.get("title") or []
        doi = (item.get("DOI") or "").strip()
        if not titles or not doi:
            continue
        authors = ", ".join(
            f"{a.get('family', '')} {a.get('given', '')}".strip()
            for a in item.get("author") or []
            if a.get("family") or a.get("given")
        )
        container = item.get("container-title") or []
        papers.append(Paper(
            title=_strip_jats(str(titles[0])),
            abstract=_strip_jats(item.get("abstract") or ""),
            authors=authors,
            journal=str(container[0]).strip() if container else journal_fallback,
            date=_crossref_date(item),
            doi=doi,
            url=f"https://doi.org/{doi}",
        ))
    return papers


def fetch_crossref_works(issn: str, from_date: str, to_date: str, rows: int) -> list[dict]:
    """按 ISSN 查 Crossref 某刊的出版日期窗口内 works（新的在前），返回原始条目。"""
    resp = requests.get(
        f"{CROSSREF_BASE}/journals/{issn}/works",
        params={
            "filter": f"from-pub-date:{from_date},until-pub-date:{to_date},type:journal-article",
            "rows": rows,
            "sort": "published",
            "order": "desc",
            "select": "DOI,title,author,published,published-online,issued,container-title,abstract,type",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["message"]["items"]


def fetch_crossref_journals(issn_map: dict[str, str], days: int,
                            rows_per_journal: int = 20) -> list[Paper]:
    """Crossref 直采：逐刊按出版日期窗口抓最新论文（不去重，调用方统一去重）。

    窗口起点对齐到月初：Cell 等刊在 Crossref 只登记"年-月"精度（如 2026-08），
    日级 from-pub-date 会把这些条目排除在窗外；放宽到月初后的重叠由 DOI 去重
    与跨天去重消化。单刊异常记日志后继续其余刊。
    """
    to_date = date.today()
    from_date = (to_date - timedelta(days=max(days, 1))).replace(day=1)
    papers: list[Paper] = []
    for name, issn in issn_map.items():
        try:
            items = fetch_crossref_works(issn, from_date.isoformat(),
                                         to_date.isoformat(), rows_per_journal)
            got = parse_crossref_items(items, journal_fallback=name)
            if got:
                logger.info("Crossref 直采：%s 命中 %d 篇", name, len(got))
                papers.extend(got)
        except Exception:
            logger.warning("Crossref 直采：%s 检索失败，跳过该刊", name, exc_info=True)
        time.sleep(0.4)
    return papers
