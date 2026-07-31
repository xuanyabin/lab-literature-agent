import json
from datetime import date, datetime, timezone

import pytest

import feedback.learner as learner
from database.db import (
    connect, get_learned_term, get_unprocessed_feedback, save_feedback, save_paper,
)
from feedback import store
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


@pytest.fixture()
def store_dir(tmp_path):
    """隔离的反馈文件队列目录（不碰仓库真实的 feedback_data/）。"""
    return tmp_path / "feedback_data"


def _paper(conn, title, abstract, doi):
    return save_paper(conn, Paper(title=title, abstract=abstract, authors="",
                                  journal="", date="2026-07-22", doi=doi, url=""))


def _feedback(conn, store_dir, user_email, paper_id, value):
    """模拟 collector 双写：pending 文件队列（学习用）+ feedback 表（统计用）。"""
    store.save_pending({"user_email": user_email, "paper_id": paper_id, "value": value},
                       base_dir=store_dir)
    save_feedback(conn, user_email, paper_id, value)


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


def test_new_term_promoted_only_after_two_positive_papers(conn, tmp_path, store_dir):
    llm = StubLLM('["spatial transcriptomics"]')
    p1 = _paper(conn, "Paper one", "about brains", "10.1/a")
    p2 = _paper(conn, "Paper two", "about hearts", "10.1/b")
    today = __import__("datetime").date(2026, 7, 23)

    _feedback(conn, store_dir, USER["email"], p1, "4")
    learner.learn_from_feedback(conn, USER, llm, CFG, today, base_dir=store_dir)
    row = get_learned_term(conn, USER["email"], "spatial transcriptomics")
    assert (row["support"], row["weight"]) == (1, 0.0)  # 候选：不提权
    assert load_active_terms(conn, USER["email"], CFG, today) == []

    _feedback(conn, store_dir, USER["email"], p2, "5")
    learner.learn_from_feedback(conn, USER, llm, CFG, today, base_dir=store_dir)
    row = get_learned_term(conn, USER["email"], "spatial transcriptomics")
    assert (row["support"], row["weight"]) == (2, CFG["initial_weight"])
    assert load_active_terms(conn, USER["email"], CFG, today) == [
        ("spatial transcriptomics", CFG["initial_weight"])]

    actions = [r["action"] for r in _audit_actions(tmp_path)]
    assert actions == ["candidate", "promote"]


def test_existing_term_boosted_and_capped(conn, store_dir):
    cfg = {**CFG, "initial_weight": 1.0, "boost": 0.5, "max_weight": 1.2, "promote_support": 1}
    llm = StubLLM('["deep learning"]')
    for i in range(4):
        pid = _paper(conn, f"Paper {i}", "deep learning model", f"10.1/{i}")
        _feedback(conn, store_dir, USER["email"], pid, "4")
        learner.learn_from_feedback(conn, USER, llm, cfg, base_dir=store_dir)
    row = get_learned_term(conn, USER["email"], "deep learning")
    assert row["support"] == 4
    assert row["weight"] == 1.2  # 封顶 max_weight


def test_textually_matched_term_reinforced_without_llm(conn, store_dir):
    llm = StubLLM("[]")
    pid = _paper(conn, "Atlas", "A single-cell ATAC-seq study", "10.1/atac")
    _feedback(conn, store_dir, USER["email"], pid, "5")
    # 先人工放入一个已提权词
    from database.db import upsert_learned_term
    upsert_learned_term(conn, USER["email"], "single-cell atac-seq", 1.0, 2, "2026-07-22")
    learner.learn_from_feedback(conn, USER, llm, CFG, base_dir=store_dir)
    row = get_learned_term(conn, USER["email"], "single-cell atac-seq")
    assert row["support"] == 3
    assert row["weight"] == 1.5
    assert llm.calls == 1  # 提词仍调用，但命中已有词走的是文本匹配


def test_weak_negative_feedback_downweights_only_learned_terms(conn, tmp_path, store_dir):
    from database.db import upsert_learned_term
    upsert_learned_term(conn, USER["email"], "hormone", 2.0, 4, "2026-07-22")
    pid = _paper(conn, "Hormone paper", "hormone signaling in honeybee", "10.1/neg")
    _feedback(conn, store_dir, USER["email"], pid, "2")
    learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG, base_dir=store_dir)

    row = get_learned_term(conn, USER["email"], "hormone")
    assert row["weight"] == 2.0 * CFG["negative_factor_weak"]
    assert row["support"] == 4  # support 不变
    # 手配词表不受影响（honeybee 命中论文但未生成学习词、不写 exclude）
    assert get_learned_term(conn, USER["email"], "honeybee") is None
    assert USER["exclude"] == []
    records = _audit_actions(tmp_path)
    assert [r["action"] for r in records] == ["downweight"]


def test_neutral_feedback_skipped_and_marked_processed(conn, store_dir):
    pid = _paper(conn, "Paper", "abstract", "10.1/read")
    _feedback(conn, store_dir, USER["email"], pid, "3")
    stats = learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG, base_dir=store_dir)
    assert stats == {"positive": 0, "negative": 0, "skipped": 1}
    assert get_unprocessed_feedback(conn, USER["email"]) == []  # db 侧已标记 processed
    # 文件队列侧同步归档：pending 清空，按月移入 processed/YYYY-MM/
    assert list((store_dir / "pending").glob("*.yaml")) == []
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert len(list((store_dir / "processed" / month).glob("*.yaml"))) == 1


def test_save_feedback_is_idempotent(conn):
    pid = _paper(conn, "Paper", "abstract", "10.1/dup")
    assert save_feedback(conn, USER["email"], pid, "5", "原因") is True
    assert save_feedback(conn, USER["email"], pid, "5", "原因") is False


def test_strong_negative_feedback_uses_strong_factor(conn, tmp_path, store_dir):
    """⭐1 强负反馈：命中的学习词 ×negative_factor_strong，首次不写 exclude_candidate。"""
    from database.db import upsert_learned_term
    upsert_learned_term(conn, USER["email"], "hormone", 2.0, 4, "2026-07-22")
    pid = _paper(conn, "Hormone paper", "hormone signaling", "10.1/strong")
    _feedback(conn, store_dir, USER["email"], pid, "1")
    stats = learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG, base_dir=store_dir)

    assert stats["negative"] == 1
    row = get_learned_term(conn, USER["email"], "hormone")
    assert row["weight"] == round(2.0 * CFG["negative_factor_strong"], 4)
    assert [r["action"] for r in _audit_actions(tmp_path)] == ["downweight"]


def test_second_strong_negative_writes_exclude_candidate(conn, tmp_path, store_dir):
    """同一（用户, 词）累计第 2 次 ⭐1 时，审计日志追加 exclude_candidate 记录。"""
    from database.db import upsert_learned_term
    upsert_learned_term(conn, USER["email"], "hormone", 2.0, 4, "2026-07-22")
    p1 = _paper(conn, "Hormone one", "hormone signaling", "10.1/s1")
    p2 = _paper(conn, "Hormone two", "hormone receptor", "10.1/s2")

    _feedback(conn, store_dir, USER["email"], p1, "1")
    learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG, base_dir=store_dir)
    assert [r["action"] for r in _audit_actions(tmp_path)] == ["downweight"]  # 第 1 次不触发

    _feedback(conn, store_dir, USER["email"], p2, "1")
    learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG, base_dir=store_dir)
    records = _audit_actions(tmp_path)
    assert [r["action"] for r in records] == ["downweight", "downweight", "exclude_candidate"]
    ex = records[-1]
    assert (ex["user"], ex["term"], ex["feedback"]) == (USER["email"], "hormone", "1")
    row = get_learned_term(conn, USER["email"], "hormone")
    assert row["weight"] == round(2.0 * CFG["negative_factor_strong"] ** 2, 4)


def test_pending_file_archived_after_learning(conn, store_dir):
    """学后：pending 清空、文件按月归档 processed/YYYY-MM/、db 行 processed=1。"""
    pid = _paper(conn, "Paper", "about single-cell", "10.1/arch")
    _feedback(conn, store_dir, USER["email"], pid, "5")
    learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG, base_dir=store_dir)

    assert list((store_dir / "pending").glob("*.yaml")) == []
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    archived = list((store_dir / "processed" / month).glob("*.yaml"))
    assert len(archived) == 1
    row = conn.execute("SELECT processed FROM feedback WHERE paper_id=?", (pid,)).fetchone()
    assert row["processed"] == 1


def test_feedback_for_missing_paper_archived_and_skipped(conn, store_dir):
    """反馈指向不存在的论文：计 skipped、仍归档（不堵队列）、db 行标 processed。"""
    _feedback(conn, store_dir, USER["email"], 99999, "4")
    stats = learner.learn_from_feedback(conn, USER, StubLLM("[]"), CFG, base_dir=store_dir)
    assert stats == {"positive": 0, "negative": 0, "skipped": 1}
    assert list((store_dir / "pending").glob("*.yaml")) == []
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert len(list((store_dir / "processed" / month).glob("*.yaml"))) == 1
