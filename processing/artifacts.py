"""全局产物层：并集论文的 LLM 产物（AI 分析/新闻摘要/中文翻译）一次生成、全用户复用。

ensure_artifacts 分三段执行（SQLite 连接不跨线程，全部库读写都留在主线程）：
  1. 主线程逐篇解析 paper_id 并读缓存（全局共享表，历史上处理过就不再调 LLM）；
  2. 缓存未命中的论文交给线程池并发调 LLM（每篇内部仍按 分析→新闻→翻译 顺序，
     新闻摘要依赖分析结果；worker 只调 LLM，不碰数据库）；
  3. 主线程把新生成的产物写回缓存并组装返回。

LLM 日预算耗尽（BudgetExhaustedError）一律向上传播——快速失败、不发空壳邮件；
其余异常逐篇回退为空产物，保证不丢篇。
"""

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

from database.db import (
    dedup_key, get_analysis, get_news_summary, get_paper_id, get_translation,
    save_analysis, save_news_summary, save_paper, save_translation,
)
from processing.analyzer import EMPTY_ANALYSIS, analyze_paper
from processing.llm import BudgetExhaustedError
from processing.paper_news_generator import generate_summary
from processing.translator import EMPTY_TRANSLATION, translate_paper
from sources.paper import Paper


def _analysis_nonempty(analysis: dict) -> bool:
    """缓存命中但内容全空（历史上失败时写入的空壳）不算有效，重新调 LLM。"""
    return any(analysis.get(k) for k in ("problem", "solution", "finding", "methods", "organisms"))


def _translation_nonempty(translation: dict) -> bool:
    """四段摘要（背景/方法/结果/意义）任一非空才算有效缓存，否则重新调 LLM。"""
    return any(translation.get(k) for k in ("background", "methods", "results", "significance"))


def _generate(paper: Paper, analysis: dict | None, needs: dict, llm,
              log: logging.Logger) -> tuple:
    """单篇论文的 LLM 产物生成（worker 线程内执行，只调 LLM 不碰数据库）。

    返回 (analysis, news, translation, generated)：未需要的字段返回 None；
    generated 记录实际调用成功的产物名（用于回写缓存——异常回退的空产物不写缓存，
    与历来语义一致）；BudgetExhaustedError 向上传播。
    """
    generated = set()
    news = translation = None
    if needs["analysis"]:
        try:
            analysis = analyze_paper(paper, llm)
            generated.add("analysis")
        except BudgetExhaustedError:
            raise
        except Exception:
            log.warning("AI 分析失败，回退为空分析：%s", paper.title[:60], exc_info=True)
            analysis = dict(EMPTY_ANALYSIS)
    if needs["news"]:
        try:
            news = generate_summary(paper, analysis, llm)
            generated.add("news")
        except BudgetExhaustedError:
            raise
        except Exception:
            log.warning("新闻摘要生成失败，回退为空：%s", paper.title[:60], exc_info=True)
            news = ""
    if needs["translation"]:
        try:
            translation = translate_paper(paper, llm)
            generated.add("translation")
        except BudgetExhaustedError:
            raise
        except Exception:
            log.warning("翻译失败，回退为空：%s", paper.title[:60], exc_info=True)
            translation = dict(EMPTY_TRANSLATION)
    return analysis, news, translation, generated


def ensure_artifacts(papers: list[Paper], llm, conn: sqlite3.Connection,
                     persist: bool, show_translation: bool,
                     log: logging.Logger, max_workers: int = 8) -> dict:
    """返回 {dedup_key: {"paper_id", "analysis", "news", "title_zh",
    "background", "methods", "results", "significance"}}。

    persist 时论文入库取 paper_id 并把新生成的产物写回缓存；dry-run（persist=False）
    只按 dedup_key 查已有 id（可能为 None），不写库。
    max_workers > 1 时未命中缓存的论文并发调 LLM（库读写仍在主线程）。
    """
    artifacts = {}
    pending = []  # 缓存未命中、需调 LLM 的论文：(paper, key, 已有 analysis 或 None, needs)
    total = len(papers)
    for i, paper in enumerate(papers, 1):
        key = dedup_key(paper)
        log.info("[%d/%d] %s", i, total, paper.title[:60])
        paper_id = save_paper(conn, paper) if persist else get_paper_id(conn, key)

        analysis = get_analysis(conn, paper_id) if paper_id is not None else None
        if analysis is not None and not _analysis_nonempty(analysis):
            analysis = None
        news = get_news_summary(conn, paper_id) if paper_id is not None else None
        translation = get_translation(conn, paper_id) if paper_id is not None else None
        if translation is not None and not _translation_nonempty(translation):
            translation = None

        artifacts[key] = {
            "paper_id": paper_id,
            "analysis": analysis or dict(EMPTY_ANALYSIS),
            "news": news or "",
            "title_zh": (translation or {}).get("title_zh", ""),
            "background": (translation or {}).get("background", ""),
            "methods": (translation or {}).get("methods", ""),
            "results": (translation or {}).get("results", ""),
            "significance": (translation or {}).get("significance", ""),
        }
        needs = {
            "analysis": analysis is None,
            "news": not news,
            "translation": bool(show_translation and paper.abstract and translation is None),
        }
        if any(needs.values()):
            pending.append((paper, key, analysis, needs))

    if not pending:
        return artifacts
    log.info("产物生成：%d/%d 篇未命中缓存，并发调 LLM（workers=%d）",
             len(pending), total, min(max_workers, len(pending)))

    results = {}
    if max_workers > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as pool:
            futures = {pool.submit(_generate, paper, analysis, needs, llm, log): key
                       for paper, key, analysis, needs in pending}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()  # BudgetExhaustedError 在此上抛
    else:
        for paper, key, analysis, needs in pending:
            results[key] = _generate(paper, analysis, needs, llm, log)

    # 主线程回写：组装 artifacts 并把成功生成的产物写回缓存
    for paper, key, _, needs in pending:
        new_analysis, new_news, new_translation, generated = results[key]
        entry = artifacts[key]
        paper_id = entry["paper_id"]
        if needs["analysis"]:
            entry["analysis"] = new_analysis
            if persist and paper_id is not None and "analysis" in generated:
                save_analysis(conn, paper_id, new_analysis)
        if needs["news"]:
            entry["news"] = new_news
            if persist and paper_id is not None and "news" in generated:
                save_news_summary(conn, paper_id, new_news)
        if needs["translation"]:
            entry["title_zh"] = new_translation.get("title_zh", "")
            entry["background"] = new_translation.get("background", "")
            entry["methods"] = new_translation.get("methods", "")
            entry["results"] = new_translation.get("results", "")
            entry["significance"] = new_translation.get("significance", "")
            if persist and paper_id is not None and "translation" in generated:
                save_translation(conn, paper_id, new_translation)
    return artifacts
