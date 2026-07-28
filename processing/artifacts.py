"""全局产物层：并集论文的 LLM 产物（AI 分析/新闻摘要/中文翻译）一次生成、全用户复用。

ensure_artifacts 对各用户 shortlist 并集中的每篇论文：优先读 SQLite 缓存
（全局共享表，同一篇论文历史上处理过就不再调 LLM），未命中才调 LLM 并
（persist 时）写回缓存。LLM 日预算耗尽（BudgetExhaustedError）一律向上传播
——快速失败、不发空壳邮件；其余异常逐篇回退为空产物，保证不丢篇。
"""

import logging
import sqlite3

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


def ensure_artifacts(papers: list[Paper], llm, conn: sqlite3.Connection,
                     persist: bool, show_translation: bool,
                     log: logging.Logger) -> dict:
    """返回 {dedup_key: {"paper_id", "analysis", "news", "title_zh",
    "background", "methods", "results", "significance"}}。

    persist 时论文入库取 paper_id 并把新生成的产物写回缓存；dry-run（persist=False）
    只按 dedup_key 查已有 id（可能为 None），不写库。
    """
    artifacts = {}
    total = len(papers)
    for i, paper in enumerate(papers, 1):
        key = dedup_key(paper)
        log.info("[%d/%d] %s", i, total, paper.title[:60])
        paper_id = save_paper(conn, paper) if persist else get_paper_id(conn, key)

        analysis = get_analysis(conn, paper_id) if paper_id is not None else None
        if not (analysis and _analysis_nonempty(analysis)):
            try:
                analysis = analyze_paper(paper, llm)
                if persist and paper_id is not None:
                    save_analysis(conn, paper_id, analysis)
            except BudgetExhaustedError:
                raise
            except Exception:
                log.warning("AI 分析失败，回退为空分析：%s", paper.title[:60], exc_info=True)
                analysis = dict(EMPTY_ANALYSIS)

        news = get_news_summary(conn, paper_id) if paper_id is not None else None
        if not news:
            try:
                news = generate_summary(paper, analysis, llm)
                if persist and paper_id is not None:
                    save_news_summary(conn, paper_id, news)
            except BudgetExhaustedError:
                raise
            except Exception:
                log.warning("新闻摘要生成失败，回退为空：%s", paper.title[:60], exc_info=True)
                news = ""

        translation = dict(EMPTY_TRANSLATION)
        if show_translation and paper.abstract:
            cached = get_translation(conn, paper_id) if paper_id is not None else None
            if cached and _translation_nonempty(cached):
                translation = cached
            else:
                try:
                    translation = translate_paper(paper, llm)
                    if persist and paper_id is not None:
                        save_translation(conn, paper_id, translation)
                except BudgetExhaustedError:
                    raise
                except Exception:
                    log.warning("翻译失败，回退为空：%s", paper.title[:60], exc_info=True)
                    translation = dict(EMPTY_TRANSLATION)

        artifacts[key] = {
            "paper_id": paper_id,
            "analysis": analysis,
            "news": news,
            "title_zh": translation.get("title_zh", ""),
            "background": translation.get("background", ""),
            "methods": translation.get("methods", ""),
            "results": translation.get("results", ""),
            "significance": translation.get("significance", ""),
        }
    return artifacts
