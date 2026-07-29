"""bioRxiv 预印本采集（官方 details API + 本地关键词过滤）。

bioRxiv 不提供服务端关键词检索，只能按日期区间拉取全量元数据后在本地
过滤。主流水线用 fetch_recent_global：全部检索词扁平等权、命中任一词
即收录（与 global_pool 的 PubMed 扁平 OR 查询语义一致）；fetch_recent
是逐人遗留路径（先严格后宽松降级），仅测试与调试用。拉取结果按日期
区间做模块级缓存，多用户共享一次抓取；API 异常时记日志并返回空列表，
绝不阻断 PubMed 主流程。
"""

import logging
from datetime import date, timedelta

import requests

from .paper import Paper, expand_with_aliases

logger = logging.getLogger(__name__)

API_BASE = "https://api.biorxiv.org/details/biorxiv"

# {(from_date, to_date): [详情 dict]}：同一天所有用户共享一次全量拉取
_DETAILS_CACHE: dict[tuple[str, str], list[dict]] = {}


def _clean(terms) -> list[str]:
    return [t.strip() for t in terms or [] if t and t.strip()]


def _term_groups(user: dict) -> tuple[list[str], list[str], list[str]]:
    """返回 (物种组, 其余词组, exclude 组)，全部小写；词表构成与 pubmed.build_queries 一致。"""
    aliases = user.get("aliases") or {}
    species = expand_with_aliases(_clean(user.get("species")), aliases)
    others = expand_with_aliases(
        _clean(user.get("research_interest")) + _clean(user.get("keywords")) + _clean(user.get("methods")),
        aliases,
    )
    seen = {t.lower() for t in species + others}
    for term in _clean(t for t, _ in user.get("learned_terms") or []):
        if term.lower() not in seen:
            seen.add(term.lower())
            others.append(term)
    exclude = _clean(user.get("exclude"))
    return ([t.lower() for t in species], [t.lower() for t in others],
            [t.lower() for t in exclude])


def fetch_recent(user: dict, days: int = 1, max_results: int = 50, min_results: int = 5) -> list[Paper]:
    """获取最近 days 天内与用户相关的 bioRxiv 预印本（本地过滤，先严格后宽松）。"""
    species, others, exclude = _term_groups(user)
    if not species and not others:
        logger.info("用户配置中没有可用的检索词，bioRxiv 过滤跳过")
        return []

    to_date = date.today()
    from_date = to_date - timedelta(days=max(days - 1, 0))
    details = _fetch_details(from_date.isoformat(), to_date.isoformat())
    if not details:
        return []

    strict: list[dict] = []
    relaxed: list[dict] = []
    for item in details:
        text = " ".join([
            item.get("title") or "",
            item.get("abstract") or "",
            item.get("category") or "",
        ]).lower()
        if exclude and any(t in text for t in exclude):
            continue
        sp_hit = any(t in text for t in species)
        ot_hit = any(t in text for t in others)
        if species and others:
            if sp_hit and ot_hit:
                strict.append(item)
            elif sp_hit or ot_hit:
                relaxed.append(item)
        elif sp_hit or ot_hit:
            strict.append(item)
    if len(strict) >= min_results or not relaxed:
        picked = strict
    else:
        logger.info("bioRxiv 严格过滤仅命中 %d 篇，降级为宽松匹配", len(strict))
        picked = strict + relaxed
    logger.info("bioRxiv 本地过滤命中 %d 篇（全量 %d 篇）", len(picked), len(details))
    return [_to_paper(item) for item in picked[:max_results]]


def fetch_recent_global(species: list[str], others: list[str], days: int = 1,
                        max_results: int = 200) -> list[Paper]:
    """全局池模式的 bioRxiv 过滤：全部检索词扁平等权，命中任一词即收录
    （与 global_pool 的 PubMed 扁平 OR 查询语义一致）。词表由调用方合并全用户
    并展开 aliases 后直接传入（本函数只做小写化，不再处理 aliases /
    exclude / learned_terms）；exclude 由全局池之后的每用户粗筛各自执行。"""
    terms = [t.lower() for t in _clean(species) + _clean(others)]
    if not terms:
        logger.info("全局词表为空，bioRxiv 过滤跳过")
        return []

    to_date = date.today()
    from_date = to_date - timedelta(days=max(days - 1, 0))
    details = _fetch_details(from_date.isoformat(), to_date.isoformat())
    if not details:
        return []

    picked: list[dict] = []
    for item in details:
        text = " ".join([
            item.get("title") or "",
            item.get("abstract") or "",
            item.get("category") or "",
        ]).lower()
        if any(t in text for t in terms):
            picked.append(item)
    logger.info("bioRxiv 全局过滤命中 %d 篇（全量 %d 篇）", len(picked), len(details))
    return [_to_paper(item) for item in picked[:max_results]]


def _fetch_details(from_date: str, to_date: str) -> list[dict]:
    """按日期区间分页拉取 bioRxiv 全量详情（每页 100 条），带模块级缓存。"""
    key = (from_date, to_date)
    if key in _DETAILS_CACHE:
        return _DETAILS_CACHE[key]
    items: list[dict] = []
    cursor = 0
    try:
        while True:
            resp = requests.get(f"{API_BASE}/{from_date}/{to_date}/{cursor}", timeout=60)
            resp.raise_for_status()
            data = resp.json()
            collection = data.get("collection") or []
            items.extend(collection)
            messages = data.get("messages") or [{}]
            total = int(messages[0].get("total") or 0)
            cursor += len(collection)
            if not collection or cursor >= total:
                break
    except Exception as exc:
        logger.warning("bioRxiv 获取失败（%s），本次运行跳过该数据源", exc)
        items = []
    _DETAILS_CACHE[key] = items
    return items


def _parse_authors(raw: str) -> str:
    """bioRxiv 作者格式 "Last, F.; Last2, F2." → "Last F., Last2 F2."。"""
    names = [a.replace(", ", " ").strip() for a in (raw or "").split(";")]
    return ", ".join(n for n in names if n)


def _to_paper(item: dict) -> Paper:
    doi = (item.get("doi") or "").strip()
    return Paper(
        title=(item.get("title") or "").strip(),
        abstract=(item.get("abstract") or "").strip(),
        authors=_parse_authors(item.get("authors") or ""),
        journal="bioRxiv",
        date=(item.get("date") or "").strip(),
        doi=doi,
        url=f"https://www.biorxiv.org/content/{doi}" if doi else "",
        keywords=[],
    )
