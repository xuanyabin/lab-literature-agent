from processing.paper_news_generator import generate_summary
from sources.paper import Paper


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def _paper():
    return Paper(title="T", abstract="A", authors="", journal="Cell",
                 date="", doi="", url="")


def test_prompt_carries_analysis_fields():
    llm = FakeLLM("摘要")
    analysis = {"field": "昆虫演化", "problem": "P", "solution": "S", "finding": "F"}
    generate_summary(_paper(), analysis, llm)
    prompt = llm.prompts[0]
    for token in ("昆虫演化", "P", "S", "F", "T", "A", "Cell"):
        assert token in prompt


def test_prefix_and_quotes_stripped():
    llm = FakeLLM('"一句话总结：为解决X问题，作者利用Y方法，发现Z机制。"')
    assert generate_summary(_paper(), {}, llm) == "为解决X问题，作者利用Y方法，发现Z机制。"
