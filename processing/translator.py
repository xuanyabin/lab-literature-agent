"""论文标题中文翻译 + 摘要四段结构化梳理（背景/研究方法/研究结果/研究意义）。"""

import json
import logging

from sources.paper import Paper

from .llm import load_prompt

logger = logging.getLogger(__name__)

EMPTY_TRANSLATION = {"title_zh": "", "background": "", "methods": "",
                     "results": "", "significance": ""}


def translate_paper(paper: Paper, llm) -> dict:
    """输出 {title_zh, background, methods, results, significance}；无摘要时全空。"""
    if not paper.abstract:
        return dict(EMPTY_TRANSLATION)
    prompt = load_prompt("paper_translation").safe_substitute(
        title=paper.title,
        abstract=paper.abstract,
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
        logger.warning("翻译输出不是合法 JSON，回退为空结果：%.100s", raw)
        return dict(EMPTY_TRANSLATION)
    if not isinstance(data, dict):
        logger.warning("翻译输出不是 JSON 对象，回退为空结果：%.100s", raw)
        return dict(EMPTY_TRANSLATION)
    result = dict(EMPTY_TRANSLATION)
    for key in result:
        if isinstance(data.get(key), str):
            result[key] = data[key]
    return result
