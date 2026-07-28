from processing.translator import EMPTY_TRANSLATION, translate_paper
from sources.paper import Paper

FULL_TRANSLATION = {"title_zh": "中文标题", "background": "背景", "methods": "方法",
                    "results": "结果", "significance": "意义"}


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
    llm = FakeLLM('{"title_zh": "中文标题", "background": "背景", "methods": "方法",'
                  ' "results": "结果", "significance": "意义"}')
    assert translate_paper(_paper(), llm) == FULL_TRANSLATION
    assert "A title" in llm.prompts[0]
    assert "An abstract." in llm.prompts[0]


def test_code_fence_tolerated():
    llm = FakeLLM('```json\n{"title_zh": "T", "background": "背", "methods": "方",'
                  ' "results": "结", "significance": "义"}\n```')
    assert translate_paper(_paper(), llm) == {"title_zh": "T", "background": "背",
                                              "methods": "方", "results": "结",
                                              "significance": "义"}


def test_missing_keys_fall_back_to_empty():
    llm = FakeLLM('{"title_zh": "T", "background": "背"}')
    assert translate_paper(_paper(), llm) == {"title_zh": "T", "background": "背",
                                              "methods": "", "results": "",
                                              "significance": ""}


def test_invalid_json_falls_back():
    llm = FakeLLM("not json")
    assert translate_paper(_paper(), llm) == dict(EMPTY_TRANSLATION)


def test_non_dict_json_falls_back():
    llm = FakeLLM('["title_zh", "background"]')
    assert translate_paper(_paper(), llm) == dict(EMPTY_TRANSLATION)


def test_no_abstract_skips_llm():
    llm = FakeLLM("不应被调用")
    assert translate_paper(_paper(abstract=""), llm) == dict(EMPTY_TRANSLATION)
    assert llm.prompts == []
