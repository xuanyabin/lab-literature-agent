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
}


def analyze_paper(paper: Paper, llm) -> dict:
    """输出 {field, problem, solution, finding, methods[], organisms[]}。"""
    prompt = load_prompt("paper_analysis").safe_substitute(
        title=paper.title,
        abstract=paper.abstract or "（无摘要）",
        keywords=", ".join(paper.keywords) or "（无）",
    )
    return _parse_json(llm.complete(prompt))


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
