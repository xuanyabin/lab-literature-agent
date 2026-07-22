"""PubMed 文献采集（NCBI E-utilities：esearch + efetch）。"""

import logging
import xml.etree.ElementTree as ET

import requests

from .paper import Paper

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_MONTHS = {
    name: f"{i:02d}"
    for i, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def build_query(user: dict) -> str:
    """根据用户配置构建 PubMed 检索式（各兴趣词之间 OR）。"""
    terms = (
        user.get("research_interest", [])
        + user.get("keywords", [])
        + user.get("methods", [])
        + user.get("species", [])
    )
    terms = [t.strip() for t in terms if t and t.strip()]
    if not terms:
        raise ValueError("用户配置中没有可用的检索词")
    return " OR ".join(f'"{t}"' for t in terms)


def fetch_recent(user: dict, days: int = 1, retmax: int = 50) -> list[Paper]:
    """获取最近 days 天内与用户相关的 PubMed 论文（已去重）。"""
    query = build_query(user)
    pmids = _esearch(query, days, retmax)
    if not pmids:
        logger.info("PubMed 未检索到新论文")
        return []
    logger.info("PubMed 检索命中 %d 篇", len(pmids))
    return dedupe(parse_efetch_xml(_efetch(pmids)))


def _esearch(query: str, days: int, retmax: int) -> list[str]:
    resp = requests.get(
        f"{EUTILS_BASE}/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "datetype": "pdat",
            "reldate": days,
            "retmax": retmax,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["esearchresult"].get("idlist", [])


def _efetch(pmids: list[str]) -> str:
    resp = requests.get(
        f"{EUTILS_BASE}/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


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

    return Paper(
        title=_text(article.find(".//ArticleTitle")),
        abstract=abstract,
        authors=", ".join(authors),
        journal=_text(article.find(".//Journal/Title")),
        date=_parse_date(article),
        doi=doi,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        keywords=[k for k in keywords if k],
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
