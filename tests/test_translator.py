from processing.translator import translate_paper
from sources.paper import Paper


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def _paper(abstract="An abstract."):
    return Paper(title="A title", abstract=abstract, authors="", journal="",
                 date="", doi="", url="")


def test_translation_parsed():
    llm = FakeLLM('{"title_zh": "中文标题", "abstract_zh": "中文摘要"}')
    assert translate_paper(_paper(), llm) == {"title_zh": "中文标题", "abstract_zh": "中文摘要"}
    assert "A title" in llm.prompts[0]
    assert "An abstract." in llm.prompts[0]


def test_code_fence_tolerated():
    llm = FakeLLM('```json\n{"title_zh": "T", "abstract_zh": "A"}\n```')
    assert translate_paper(_paper(), llm) == {"title_zh": "T", "abstract_zh": "A"}


def test_invalid_json_falls_back():
    llm = FakeLLM("not json")
    assert translate_paper(_paper(), llm) == {"title_zh": "", "abstract_zh": ""}


def test_no_abstract_skips_llm():
    llm = FakeLLM("不应被调用")
    assert translate_paper(_paper(abstract=""), llm) == {"title_zh": "", "abstract_zh": ""}
    assert llm.prompts == []
