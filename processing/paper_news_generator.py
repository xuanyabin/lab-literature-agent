"""Module 3：Research News Generator —— 生成一句话中文科研新闻摘要（50–80 字）。"""

from sources.paper import Paper

from .llm import load_prompt

_PREFIXES = ("一句话总结：", "一句话总结:", "摘要：", "摘要:")


def generate_summary(paper: Paper, analysis: dict, llm) -> str:
    """结合摘要分析结果生成一句话科研新闻摘要。"""
    prompt = load_prompt("paper_news_summary").safe_substitute(
        title=paper.title,
        journal=paper.journal or "（未知期刊）",
        field=analysis.get("field", ""),
        problem=analysis.get("problem", ""),
        solution=analysis.get("solution", ""),
        finding=analysis.get("finding", ""),
        abstract=paper.abstract or "（无摘要）",
    )
    return _clean(llm.complete(prompt))


def _clean(summary: str) -> str:
    """剥掉模型偶尔多加的引号与"一句话总结："等前缀。"""
    text = summary.strip().strip('"').strip()
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text
