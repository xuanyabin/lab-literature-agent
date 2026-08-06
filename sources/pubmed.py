"""PubMed 文献采集（NCBI E-utilities：esearch + efetch）。"""

import logging
import time
import xml.etree.ElementTree as ET

import requests

from .paper import Paper, expand_with_aliases

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_TOOL = "lab-literature-intelligence"

_MONTHS = {
    name: f"{i:02d}"
    for i, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def _clean(terms) -> list[str]:
    return [t.strip() for t in terms or [] if t and t.strip()]


def _or(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms)


def build_queries(user: dict) -> tuple[str, str]:
    """返回 (严格查询, 宽松查询)。

    严格查询：物种组 OR 合并后与其余检索词 AND，缩小明显不相关的命中；
    宽松查询：全部检索词扁平 OR，严格查询命中过少时降级使用。
    检索词会先并入用户 aliases 中的语义拓展词，再并入反馈学习词表中
    当前有效的学习词（Phase 5，user["learned_terms"] 为 [(词, 有效权重)]）；
    两者都会附加用户 exclude 词的 NOT 排除。
    """
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
    if not species and not others:
        raise ValueError("用户配置中没有可用的检索词")

    relaxed = _or(species + others)
    strict = relaxed
    if species and others:
        strict = f"({_or(species)}) AND ({_or(others)})"

    exclude = _clean(user.get("exclude"))
    if exclude:
        strict = f"({strict}) NOT ({_or(exclude)})"
        relaxed = f"({relaxed}) NOT ({_or(exclude)})"
    return strict, relaxed


def fetch_recent(user: dict, days: int = 1, retmax: int = 50, min_results: int = 5) -> list[Paper]:
    """获取最近 days 天内与用户相关的 PubMed 论文（已去重）。

    先用严格查询检索；命中不足 min_results 且存在更宽松的查询时自动降级，
    保证每天有足够候选进入后续打分。
    """
    strict, relaxed = build_queries(user)
    pmids = _esearch(strict, days, retmax)
    if len(pmids) < min_results and relaxed != strict:
        logger.info("严格查询仅命中 %d 篇，降级为宽松查询重试", len(pmids))
        pmids = _esearch(relaxed, days, retmax)
    if not pmids:
        logger.info("PubMed 未检索到新论文")
        return []
    logger.info("PubMed 检索命中 %d 篇", len(pmids))
    return dedupe(parse_efetch_xml(_efetch(pmids)))


def search_pmids(query: str, days: int, retmax: int) -> list[str]:
    """按查询式检索 PubMed，返回 PMID 列表（全局池按簇检索的公开包装）。"""
    return _esearch(query, days, retmax)


def count_pmids(query: str, days: int) -> int:
    """按查询式检索 PubMed，只返回命中总数（retmax=0，不下载 PMID 列表）。"""
    resp = _get_with_retry(
        f"{EUTILS_BASE}/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "datetype": "pdat",
            "reldate": days,
            "retmax": 0,
            "tool": _TOOL,
        },
        timeout=30,
    )
    return int(resp.json()["esearchresult"].get("count", 0))


def fetch_by_pmids(pmids: list[str]) -> list[Paper]:
    """按 PMID 列表取回论文详情（不去重，全局去重由调用方统一做）。"""
    if not pmids:
        return []
    return parse_efetch_xml(_efetch(pmids))


def pmid_for_doi(doi: str) -> str | None:
    """经 PubMed esearch 把 DOI 转换为 PMID；查不到返回 None。

    供"文献输入优化关键词"队列消费：用户粘贴 DOI 时先转成 PMID 再 efetch。
    与 _esearch 不同，这里不限定日期窗口（文献可以是任意年份）。
    """
    doi = (doi or "").strip()
    if not doi:
        return None
    resp = _get_with_retry(
        f"{EUTILS_BASE}/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": f"{doi}[doi]",
            "retmode": "json",
            "retmax": 1,
            "tool": _TOOL,
        },
        timeout=30,
    )
    ids = resp.json()["esearchresult"].get("idlist", [])
    return ids[0] if ids else None


def _esearch(query: str, days: int, retmax: int) -> list[str]:
    resp = _get_with_retry(
        f"{EUTILS_BASE}/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "datetype": "pdat",
            "reldate": days,
            "retmax": retmax,
            "tool": _TOOL,
        },
        timeout=30,
    )
    return resp.json()["esearchresult"].get("idlist", [])


def _efetch(pmids: list[str]) -> str:
    resp = _get_with_retry(
        f"{EUTILS_BASE}/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml", "tool": _TOOL},
        timeout=60,
    )
    return resp.text


def _get_with_retry(url: str, params: dict, timeout: int, retries: int = 3):
    """NCBI 限流（429）时指数退避重试，其余错误直接抛出。"""
    for attempt in range(retries + 1):
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        if attempt == retries:
            resp.raise_for_status()
        wait = 5 * (2 ** attempt)
        logger.warning("PubMed 限流（429），%d 秒后重试（%d/%d）", wait, attempt + 1, retries)
        time.sleep(wait)


def parse_efetch_xml(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    return [_parse_article(article) for article in root.iter("PubmedArticle")]


def dedupe(papers: list[Paper]) -> list[Paper]:
    """按 DOI（优先）或规范化标题去重。"""
    seen: set[str] = set()
    unique: list[Paper] = []
    for p in papers:
        key = p.doi.lower() if p.doi else " ".join(p.title.lower().split())
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    if len(unique) < len(papers):
        logger.info("去重移除 %d 篇", len(papers) - len(unique))
    return unique


def _text(node) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _parse_article(article) -> Paper:
    pmid = _text(article.find(".//PMID"))

    doi = ""
    for aid in article.iter("ArticleId"):
        if aid.get("IdType") == "doi":
            doi = _text(aid)
            break

    abstract = " ".join(
        _text(t) for t in article.findall(".//Abstract/AbstractText")
    ).strip()

    authors = []
    for author in article.findall(".//AuthorList/Author"):
        name = f"{_text(author.find('LastName'))} {_text(author.find('ForeName'))}".strip()
        if not name:
            name = _text(author.find("CollectiveName"))
        if name:
            authors.append(name)

    keywords = [_text(k) for k in article.findall(".//KeywordList/Keyword")]

    publication_types = [
        _text(pt) for pt in article.findall(".//PublicationTypeList/PublicationType")
    ]

    return Paper(
        title=_text(article.find(".//ArticleTitle")),
        abstract=abstract,
        authors=", ".join(authors),
        journal=_text(article.find(".//Journal/Title")),
        date=_parse_date(article),
        doi=doi,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        keywords=[k for k in keywords if k],
        publication_types=[pt for pt in publication_types if pt],
    )


def _parse_date(article) -> str:
    """优先取 ArticleDate（数值日期），否则回退到 JournalIssue/PubDate。"""
    node = article.find(".//ArticleDate")
    if node is not None:
        year = _text(node.find("Year"))
        month = _text(node.find("Month"))
        day = _text(node.find("Day"))
        if year and month and day:
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    node = article.find(".//JournalIssue/PubDate")
    if node is not None:
        year = _text(node.find("Year"))
        month = _text(node.find("Month"))
        day = _text(node.find("Day")) or "01"
        month = _MONTHS.get(month[:3], month.zfill(2) if month.isdigit() else "01")
        if year:
            return f"{year}-{month}-{day.zfill(2)}"
    return ""
