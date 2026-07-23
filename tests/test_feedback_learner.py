import json
from datetime import date

import pytest

import feedback.learner as learner
from database.db import (
    connect, get_learned_term, get_unprocessed_feedback, save_feedback, save_paper,
)
from feedback.vocab import DEFAULT_LEARNED_CONFIG, load_active_terms
from sources.paper import Paper

USER = {
    "name": "Tester",
    "email": "a@x.com",
    "research_interest": ["insect evolution"],
    "keywords": [],
    "methods": ["single-cell RNA sequencing"],
    "species": ["honeybee"],
    "exclude": [],
}

CFG = dict(DEFAULT_LEARNED_CONFIG)


class StubLLM:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return self.output


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(learner, "AUDIT_LOG", tmp_path / "audit.log")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def _paper(conn, title, abstract, doi):
    return save_paper(conn, Paper(title=title, abstract=abstract, authors="",
                                  journal="", date="2026-07-22", doi=doi, url=""))


def _audit_actions(tmp_path):
    log = tmp_path / "audit.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_extract_terms_parses_and_dedupes():
    llm = StubLLM('```json\n["ATAC-seq", " Honeybee ", "atac-seq", ""]\n```')
    terms = learner.extract_terms("t", "a", ["honeybee"], llm)
    assert terms == ["atac-seq"]  # 去重、去已有词、去空串、统一小写


def test_extract_terms_invalid_output_returns_empty():
    assert learner.extract_terms("t", "a", [], StubLLM("not json")) == []
    assert learner.extract_terms("t", "a", [], StubLLM('{"x": 1}')) == []


def test_new_term_promoted_only_after_two_positive_papers(conn, tmp_path):
    llm = StubLLM('["spatial transcriptomics"]')
    p1 = _paper(conn, "Paper one", "about brains", "10.1/a")
    p2 = _paper(conn, "Paper two", "about hearts", "10.1/b")
    today = __import__("datetime").date(2026, 7, 23)

    save_feedback(conn, USER["email"], p1, "relevant")
    learner.learn_from_feedback(conn, USER, llm, CFG, today)
    row = get_learned_term(conn, USER["email"], "spatial transcriptomics")
    assert (row["support"], row["weight"]) == (1, 0.0)  # 候选：不提权
    assert load_active_terms(conn, USER["email"], CFG, today) == []

    save_feedback(conn, USER["email"], p2, "save")
    learner.learn_from_feedback(conn, USER, llm, CFG, today)
    row = get_learned_term(conn, USER["email"], "spatial transcriptomics")
    assert (row["support"], row["weight"]) == (2, CFG["initial_weight"])
    assert load_active_terms(conn, USER["email"], CFG, today) == [
        ("spatial transcriptomics", CFG["initial_weight"])]

    actions = [r["action"] for r in _audit_actions(tmp_path)]
    assert actions == ["candidate", "promote"]


def test_existing_term_boosted_and_capped(conn):
    cfg = {**CFG, "initial_weight": 1.0, "boost": 0.5, "max_weight": 1.2, "promote_support": 1}
    llm = StubLLM('["deep learning"]')
    for i in range(4):
        pid = _paper(conn, f"Paper {i}", "deep learning model", f"10.1/{i}")
        save_feedback(conn, USER["email"], pid, "relevant")
        learner.learn_from_feedback(conn, USER, llm, cfg)
    row = get_learned_term(conn, USER["email"], "deep learning")
    assert row["support"] == 4
    assert row["weight"] == 1.2  # 封顶 max_weight


def test_textually_matched_term_reinforced_without_llm(conn):
    llm = StubLLM("[]")
    pid = _paper(conn, "Atlas", "A single-cell ATAC-seq study", "10.1/atac")
    save_feedback(conn, USER["email"], pid, "relevant")
    # 先人工放入一个已提权词
    from database.db import upsert_learned_term
    upsert_learned_term(conn, USER["email"], "single-cell atac-seq", 1.0, 2, "2026-07-22")
    learner.learn_from_feedback(conn, USER, llm, CFG)
    row = get_learned_term(conn, USER["email"], "single-cell atac-seq")
    assert row["support"] == 3
    assert row["weight"] == 1.5
    assert llm.calls == 1  # 提词仍调用，但命中已有词走的是文本匹配


def test_negative_feedback_downweights_only_learned_terms(conn, tmp_path):
    from database.db import upsert_learned_term
    upsert_learned_term(conn, USER["email"], "hormone", 2.0, 4, "2026-07-22")
    pid = _paper(conn, "Hormone paper", "hormone signaling in honeybee", "10.1/neg")
    save_feedback(conn, USER["email"], pid, "not_relevant")
    learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG)

    row = get_learned_term(conn, USER["email"], "hormone")
    assert row["weight"] == 2.0 * CFG["negative_factor"]
    assert row["support"] == 4  # support 不变
    # 手配词表不受影响（honeybee 命中论文但未生成学习词、不写 exclude）
    assert get_learned_term(conn, USER["email"], "honeybee") is None
    assert USER["exclude"] == []
    records = _audit_actions(tmp_path)
    assert [r["action"] for r in records] == ["downweight"]


def test_already_read_skipped_and_feedback_marked_processed(conn):
    pid = _paper(conn, "Paper", "abstract", "10.1/read")
    save_feedback(conn, USER["email"], pid, "already_read")
    stats = learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG)
    assert stats == {"positive": 0, "negative": 0, "skipped": 1}
    assert get_unprocessed_feedback(conn, USER["email"]) == []  # 已标记 processed


def test_save_feedback_is_idempotent(conn):
    pid = _paper(conn, "Paper", "abstract", "10.1/dup")
    assert save_feedback(conn, USER["email"], pid, "relevant", "原因") is True
    assert save_feedback(conn, USER["email"], pid, "relevant", "原因") is False
