from processing.daily_summary_generator import generate_daily_summary
from sources.paper import Paper


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def _item(title, category="Reference"):
    paper = Paper(title=title, abstract="Some abstract.", authors="",
                  journal="Cell", date="", doi="", url="", keywords=["snRNA-seq"])
    return {"paper": paper, "news": "一句话摘要。", "category": category}


def test_prompt_carries_all_papers():
    llm = FakeLLM("总结")
    items = [_item("Paper A", "Must Read"), _item("Paper B")]
    generate_daily_summary(items, llm)
    prompt = llm.prompts[0]
    for token in ("Paper A", "Paper B", "Must Read", "Reference", "Cell",
                  "snRNA-seq", "一句话摘要。", "Some abstract.", "2"):
        assert token in prompt


def test_output_stripped():
    llm = FakeLLM('  "今日论文集中在单细胞组学。"  ')
    assert generate_daily_summary([_item("P")], llm) == "今日论文集中在单细胞组学。"


def test_empty_items_returns_empty():
    llm = FakeLLM("不应被调用")
    assert generate_daily_summary([], llm) == ""
    assert llm.prompts == []
