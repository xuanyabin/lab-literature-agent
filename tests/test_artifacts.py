"""processing/artifacts.py — ensure_artifacts 生成/缓存复用/降级。"""

import logging

import pytest

from database.db import (
    connect,
    dedup_key,
    get_analysis,
    get_news_summary,
    get_paper_id,
    get_translation,
    save_analysis,
    save_paper,
)
from processing.analyzer import EMPTY_ANALYSIS
from processing.artifacts import ensure_artifacts
from processing.llm import BudgetExhaustedError
from sources.paper import Paper

ANALYSIS_JSON = '{"problem":"P","solution":"S","finding":"F","methods":["m"],"organisms":["o"]}'
TRANSLATION_JSON = '{"title_zh":"题","abstract_zh":"摘"}'
LOG = logging.getLogger("test.artifacts")


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def _paper(doi="10.1/x", abstract="Some abstract"):
    return Paper(title="A Title", abstract=abstract, authors="Zhang", journal="Cell",
                 date="2026-07-22", doi=doi, url="http://x", keywords=["kw"])


KEY = "doi:10.1/x"


class TestEnsureArtifacts:
    def test_generates_then_reuses_cache(self, conn):
        papers = [_paper()]
        llm = FakeLLM([ANALYSIS_JSON, "news text", TRANSLATION_JSON])
        art = ensure_artifacts(papers, llm, conn, True, True, LOG)
        assert llm.calls == 3
        entry = art[KEY]
        assert entry["analysis"]["problem"] == "P"
        assert entry["news"] == "news text"
        assert entry["title_zh"] == "题"
        assert entry["abstract_zh"] == "摘"
        assert entry["paper_id"] is not None

        # 第二次：全部命中缓存，不再调 LLM
        llm2 = FakeLLM([])
        art2 = ensure_artifacts(papers, llm2, conn, True, True, LOG)
        assert llm2.calls == 0
        assert art2[KEY]["analysis"]["problem"] == "P"
        assert art2[KEY]["news"] == "news text"
        assert art2[KEY]["title_zh"] == "题"

    def test_per_paper_exception_falls_back(self, conn):
        llm = FakeLLM([ValueError("a"), ValueError("b"), ValueError("c")])
        art = ensure_artifacts([_paper()], llm, conn, True, True, LOG)
        entry = art[KEY]
        assert entry["analysis"] == EMPTY_ANALYSIS
        assert entry["news"] == ""
        assert entry["title_zh"] == ""
        assert entry["abstract_zh"] == ""
        # 瞬时失败不写缓存
        assert get_analysis(conn, entry["paper_id"]) is None
        assert get_news_summary(conn, entry["paper_id"]) is None
        assert get_translation(conn, entry["paper_id"]) is None

    def test_budget_exhausted_propagates(self, conn):
        llm = FakeLLM([BudgetExhaustedError("budget")])
        with pytest.raises(BudgetExhaustedError):
            ensure_artifacts([_paper()], llm, conn, True, True, LOG)

    def test_show_translation_false_skips_translation(self, conn):
        llm = FakeLLM([ANALYSIS_JSON, "news text"])
        art = ensure_artifacts([_paper()], llm, conn, False, False, LOG)
        assert llm.calls == 2
        assert art[KEY]["title_zh"] == ""
        assert art[KEY]["abstract_zh"] == ""

    def test_dry_run_reads_but_does_not_write(self, conn):
        # 预置缓存：papers 行 + analysis
        pid = save_paper(conn, _paper())
        save_analysis(conn, pid, {"problem": "cached", "solution": "", "finding": "",
                                  "methods": [], "organisms": []})
        assert pid == get_paper_id(conn, KEY)

        llm = FakeLLM(["news text", TRANSLATION_JSON])
        art = ensure_artifacts([_paper()], llm, conn, False, True, LOG)
        entry = art[KEY]
        assert entry["paper_id"] == pid                  # 复用已有行
        assert entry["analysis"]["problem"] == "cached"  # 命中缓存
        assert entry["news"] == "news text"
        assert llm.calls == 2                            # analysis 未重算
        # dry-run 不落库：news / translation 均未写入
        assert get_news_summary(conn, pid) is None
        assert get_translation(conn, pid) is None

    def test_no_abstract_skips_translation_call(self, conn):
        llm = FakeLLM([ANALYSIS_JSON, "news text"])
        art = ensure_artifacts([_paper(abstract="")], llm, conn, False, True, LOG)
        assert llm.calls == 2
        assert art[KEY]["title_zh"] == ""

    def test_dedup_key_matches_db(self):
        # 确认测试用的 KEY 常量与 db.dedup_key 规则一致
        assert dedup_key(_paper()) == KEY
