import json

from processing.analyzer import EMPTY_ANALYSIS, analyze_paper
from sources.paper import Paper


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def make_paper():
    return Paper(
        title="Single-cell atlas of the honeybee brain",
        abstract="We used snRNA-seq to profile Apis mellifera brains.",
        authors="Zhang Wei",
        journal="Nature Communications",
        date="2025-07-16",
        doi="10.1038/x",
        url="https://pubmed.ncbi.nlm.nih.gov/40123456/",
        keywords=["snRNA-seq"],
    )


def test_analyze_parses_json_and_prompt_contains_metadata():
    reply = json.dumps({
        "field": "昆虫神经科学",
        "problem": "p", "solution": "s", "finding": "f",
        "methods": ["snRNA-seq"], "organisms": ["Apis mellifera"],
    }, ensure_ascii=False)
    llm = FakeLLM(reply)
    result = analyze_paper(make_paper(), llm)
    assert result["field"] == "昆虫神经科学"
    assert result["methods"] == ["snRNA-seq"]
    assert "Single-cell atlas" in llm.prompts[0]
    assert "snRNA-seq to profile" in llm.prompts[0]


def test_analyze_strips_markdown_fence():
    llm = FakeLLM('```json\n{"field": "x", "methods": []}\n```')
    assert analyze_paper(make_paper(), llm)["field"] == "x"


def test_analyze_invalid_json_falls_back_to_empty():
    llm = FakeLLM("这不是JSON")
    assert analyze_paper(make_paper(), llm) == EMPTY_ANALYSIS
