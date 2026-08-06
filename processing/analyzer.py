"""Module 2：Abstract Understanding Engine —— 仅基于标题/关键词/摘要，禁止幻觉。"""

import json
import logging

from sources.paper import Paper

from .llm import load_prompt

logger = logging.getLogger(__name__)

EMPTY_ANALYSIS = {
    "field": "",
    "problem": "",
    "solution": "",
    "finding": "",
    "methods": [],
    "organisms": [],
    # 文献类型：方法学 / 研究 / 综述 三值；"" 表示未知（不渲染标签）。
    "paper_type": "",
}

#: paper_type 合法取值；LLM 输出其他值一律按解析失败处理（回退 ""）。
PAPER_TYPES = ("方法学", "研究", "综述")


def analyze_paper(paper: Paper, llm) -> dict:
    """输出 {field, problem, solution, finding, methods[], organisms[], paper_type}。"""
    prompt = load_prompt("paper_analysis").safe_substitute(
        title=paper.title,
        abstract=paper.abstract or "（无摘要）",
        keywords=", ".join(paper.keywords) or "（无）",
    )
    result = _parse_json(llm.complete(prompt))
    # 抓取元数据（如 PubMed PublicationType）已标明 Review 时优先采信，
    # LLM 只在无此元数据时判断；非法值按解析失败回退为 ""。
    if any("review" in str(pt).lower() for pt in paper.publication_types):
        result["paper_type"] = "综述"
    elif result.get("paper_type") not in PAPER_TYPES:
        if result.get("paper_type"):
            logger.warning("paper_type 非法值 %r，按解析失败回退为空", result["paper_type"])
        result["paper_type"] = ""
    return result


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("AI 分析输出不是合法 JSON，回退为空结果：%.100s", raw)
        return dict(EMPTY_ANALYSIS)
    result = dict(EMPTY_ANALYSIS)
    for key in result:
        if key in data:
            result[key] = data[key]
    return result
