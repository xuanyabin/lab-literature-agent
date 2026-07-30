"""全局摄取层：全用户词表合并 → LLM 主题聚类（缓存）→ 分批检索合并全局池。

多用户共享一次检索：collect_global_terms 把所有 active 用户的检索词
（aliases 已展开、含反馈学习词与实验室公共方向 lab_topics）合并去重，
cluster_terms 调 LLM 按研究主题聚成若干簇（结果按词表哈希缓存 7 天，
LLM 失败沿用旧缓存，无缓存回退单簇），fetch_global_pubmed 逐簇检索后
合并去重，与 bioRxiv 全局过滤结果一起构成当日全局池。exclude 词不进全局
检索（各用户粗筛时各自剔除），查询式永不带 NOT。可选顶刊直采通道
（sources/top_journals.py）：按 journals.yaml 刊名绕过关键词召回直抓
最新论文并入池中，解决顶刊漏召回。
"""

import hashlib
import json
import logging
import time
from pathlib import Path

import yaml

from processing.llm import load_prompt
from sources import biorxiv, pubmed, top_journals
from sources.paper import Paper, expand_with_aliases

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CLUSTER_CACHE = BASE_DIR / "config" / "users" / "auto_terms" / "_clusters.yaml"
CLUSTER_TTL = 7 * 86400


def _clean(terms) -> list[str]:
    """过滤空串与非字符串项，并去掉首尾空白。"""
    return [t.strip() for t in terms or [] if isinstance(t, str) and t.strip()]


def _unique(terms: list[str]) -> list[str]:
    """大小写不敏感去重，保持原有顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _or(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms)


def collect_global_terms(prepared_users: list[dict]) -> dict:
    """合并全部用户的检索词：species 进物种组，research_interest/keywords/methods、
    实验室公共方向 lab_topics 与反馈学习词进其余组；aliases 先展开，大小写去重、
    保持顺序。lab_topics 同时参与召回与打分（V4 起，确保实验室方向不漏文献）。"""
    species_all: list[str] = []
    others_all: list[str] = []
    for user in prepared_users:
        aliases = user.get("aliases") or {}
        species_all += expand_with_aliases(_clean(user.get("species")), aliases)
        others_all += expand_with_aliases(
            _clean(user.get("research_interest")) + _clean(user.get("keywords"))
            + _clean(user.get("methods")),
            aliases,
        )
        others_all += _clean(user.get("lab_topics"))
        others_all += _clean(t for t, _ in user.get("learned_terms") or [])
    return {"species": _unique(species_all), "others": _unique(others_all)}


def _terms_hash(terms: dict) -> str:
    """词表指纹：全小写排序后的 sha1 前 12 位，词表不变则聚类缓存直接复用。"""
    blob = "\n".join(sorted(t.lower() for t in terms["species"] + terms["others"]))
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def load_clusters(path: Path) -> dict | None:
    """读取聚类缓存 {updated, terms_hash, clusters}；文件缺失/损坏返回 None。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("clusters"), list):
        return None
    return data


def _write_clusters(path: Path, terms_hash: str, clusters: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = "# 自动维护，请勿手改：全实验室检索词的 LLM 主题聚类缓存（词表哈希 + 7 天 TTL）\n"
    body = yaml.dump(
        {"updated": int(time.time()), "terms_hash": terms_hash, "clusters": clusters},
        allow_unicode=True, sort_keys=False,
    )
    p.write_text(header + body, encoding="utf-8")


def _parse_clusters(raw: str, terms: dict) -> list[dict]:
    """解析 LLM 聚类输出：每项必须有 species/terms 列表，且所有簇的并集
    必须覆盖全部输入词（缺词视为失败，抛 ValueError 由调用方回退）。"""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        raise ValueError("聚类输出不是非空 JSON 数组")
    clusters = []
    covered: set[str] = set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("species"), list) \
                or not isinstance(item.get("terms"), list):
            raise ValueError("聚类项缺少 species/terms 列表")
        sp = [str(t) for t in item["species"]]
        tm = [str(t) for t in item["terms"]]
        covered.update(t.lower() for t in sp + tm)
        clusters.append({"topic": str(item.get("topic") or "未命名"),
                         "species": sp, "terms": tm})
    missing = [t for t in terms["species"] + terms["others"] if t.lower() not in covered]
    if missing:
        raise ValueError(f"聚类结果未覆盖全部检索词：{missing[:5]}")
    return clusters


def cluster_terms(terms: dict, llm, cache_path: Path = CLUSTER_CACHE) -> list[dict]:
    """把全局检索词聚成若干主题簇（带缓存）。

    缓存存在且词表哈希相同且未超 7 天 → 直接复用；否则调 LLM 聚类，
    成功则写缓存；LLM 异常或输出校验失败 → 沿用旧缓存；无旧缓存时
    回退为单簇（全部词扁平检索）。
    """
    terms_hash = _terms_hash(terms)
    cached = load_clusters(cache_path)
    if cached and cached.get("terms_hash") == terms_hash \
            and time.time() - float(cached.get("updated") or 0) <= CLUSTER_TTL:
        logger.info("主题聚类缓存命中（%d 簇），跳过 LLM 调用", len(cached["clusters"]))
        return cached["clusters"]

    clusters = None
    if terms["species"] or terms["others"]:
        species_block = "\n".join(f"- {t}" for t in terms["species"]) or "- （无）"
        terms_block = "\n".join(f"- {t}" for t in terms["others"]) or "- （无）"
        prompt = load_prompt("topic_clustering").safe_substitute(
            species_block=species_block, terms_block=terms_block)
        try:
            clusters = _parse_clusters(llm.complete(prompt), terms)
        except Exception:
            logger.warning("主题聚类失败，尝试沿用旧缓存", exc_info=True)
            clusters = None
    if clusters:
        _write_clusters(cache_path, terms_hash, clusters)
        logger.info("主题聚类已刷新：%d 簇（%s）", len(clusters),
                    "、".join(c["topic"] for c in clusters))
        return clusters
    if cached and cached.get("clusters"):
        logger.info("沿用旧主题聚类缓存（%d 簇）", len(cached["clusters"]))
        return cached["clusters"]
    logger.info("无聚类缓存可用，回退为单簇扁平检索")
    return [{"topic": "全部", "species": terms["species"], "terms": terms["others"]}]


def build_cluster_query(cluster: dict) -> str:
    """单簇查询式：全部检索词扁平 OR——关键词等权、不区分物种与其他词，
    命中任一词即召回；永不带 NOT（exclude 在各用户粗筛时各自执行）。"""
    return _or(_clean(cluster.get("species")) + _clean(cluster.get("terms")))


def fetch_global_pubmed(clusters: list[dict], days: int, retmax: int = 100) -> list[Paper]:
    """逐簇检索 PubMed 并合并去重。

    单簇异常记日志后继续其余簇；簇间 sleep 0.4s 避免触发 NCBI 限流。
    """
    papers: list[Paper] = []
    for cluster in clusters:
        try:
            query = build_cluster_query(cluster)
            if not query:
                continue
            pmids = pubmed.search_pmids(query, days, retmax)
            logger.info("簇「%s」命中 %d 篇", cluster.get("topic"), len(pmids))
            papers.extend(pubmed.fetch_by_pmids(pmids))
        except Exception:
            logger.warning("簇「%s」检索失败，跳过该簇", cluster.get("topic"), exc_info=True)
        time.sleep(0.4)
    return pubmed.dedupe(papers)


def fetch_global_pool(prepared_users: list[dict], llm, days: int,
                      journal_channel: dict | None = None) -> list[Paper]:
    """全局池入口：合并词表 → 主题聚类 → PubMed 分簇检索 + bioRxiv 全局过滤
    → （可选）顶刊直采通道合并 → 去重。

    journal_channel: {"names": [刊名...], "retmax_per_journal": N}，
    刊名来自 journals.yaml（top_journals.load_journal_names），按刊直抓绕过
    关键词召回；为 None 或 names 为空时不启用。
    """
    terms = collect_global_terms(prepared_users)
    papers: list[Paper] = []
    if terms["species"] or terms["others"]:
        logger.info("全局检索词：物种 %d 个、其余 %d 个", len(terms["species"]), len(terms["others"]))
        clusters = cluster_terms(terms, llm)
        papers = fetch_global_pubmed(clusters, days)
        logger.info("PubMed 全局池 %d 篇（去重后）", len(papers))
        preprints = biorxiv.fetch_recent_global(terms["species"], terms["others"], days)
        logger.info("bioRxiv 全局池 %d 篇（本地过滤后）", len(preprints))
        papers += preprints
    else:
        logger.info("全局词表为空，跳过关键词检索")
    if journal_channel and journal_channel.get("names"):
        top = top_journals.fetch_top_journals(
            journal_channel["names"], days,
            journal_channel.get("retmax_per_journal", 20))
        logger.info("顶刊通道全局池 %d 篇", len(top))
        papers += top
    return pubmed.dedupe(papers)  # 关键词池在前，撞 DOI/标题时关键词池版本优先
