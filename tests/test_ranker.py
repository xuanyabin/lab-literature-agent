from datetime import date, timedelta

import yaml

from recommendation.ranker import (
    final_score,
    journal_influence,
    judge_paper,
    lab_relevance,
    load_ranker_thresholds,
    load_ranker_weights,
    method_relevance,
    rank_items,
    recency,
)
from sources.paper import Paper

USER = {
    "research_interest": ["insect evolution"],
    "keywords": [],
    "methods": ["single-cell RNA sequencing"],
    "species": ["honeybee"],
    "lab_topics": ["genomics", "spatial transcriptomics"],
    "aliases": {"honeybee": ["Apis mellifera"]},
}

WEIGHTS = {"personal": 20, "lab": 20, "journal": 30, "novelty": 10, "method": 10, "recency": 10}
THRESHOLDS = {"must_read": 60, "important": 20}
JOURNAL_TIERS = {"nature": "t0", "plos one": "t1"}


class FakeLLM:
    """按标题返回不同评分的假 LLM，用于验证精排逻辑。"""

    def complete(self, prompt: str) -> str:
        if "High paper" in prompt:
            return '{"personal_relevance": 100, "novelty": 100, "reason": "高相关"}'
        return '{"personal_relevance": 0, "novelty": 0, "reason": "低相关"}'


def _paper(title, abstract="", journal="", date_str=""):
    return Paper(title=title, abstract=abstract, authors="", journal=journal,
                 date=date_str, doi="", url="", keywords=[])


def test_lab_relevance_graded_by_hits():
    assert lab_relevance(_paper("Unrelated"), USER) == 0
    assert lab_relevance(_paper("A genomics study"), USER) == 50
    assert lab_relevance(_paper("Genomics", abstract="spatial transcriptomics"), USER) == 100


def test_method_relevance_uses_personal_methods():
    assert method_relevance(_paper("Unrelated"), USER) == 0
    assert method_relevance(_paper("Atlas", abstract="single-cell RNA sequencing"), USER) == 50


def test_journal_influence_by_tier():
    assert journal_influence(_paper("x", journal="Nature"), JOURNAL_TIERS) == 100
    assert journal_influence(_paper("x", journal="PLOS ONE"), JOURNAL_TIERS) == 70
    assert journal_influence(_paper("x", journal="Obscure Journal"), JOURNAL_TIERS) == 30


def test_recency_graded_by_age():
    today = date(2026, 7, 22)
    for days, expected in [(0, 100), (1, 100), (2, 80), (3, 60), (7, 40), (30, 20)]:
        d = (today - timedelta(days=days)).isoformat()
        assert recency(_paper("x", date_str=d), today) == expected
    assert recency(_paper("x", date_str="not-a-date"), today) == 50  # 解析失败回退中性分


def test_final_score_weighted_average():
    dims = {"personal": 100, "lab": 100, "journal": 100, "novelty": 100, "method": 100, "recency": 100}
    assert final_score(dims, WEIGHTS) == 100
    dims["personal"] = 0
    assert final_score(dims, WEIGHTS) == 80  # 只扣掉 personal 的 20%


def test_judge_paper_fallback_on_bad_json():
    class BadLLM:
        def complete(self, prompt):
            return "not json"

    result = judge_paper(_paper("x"), {}, USER, BadLLM())
    assert result == {"personal_relevance": 50, "novelty": 50, "reason": ""}


def test_rank_items_sorts_by_final_score_and_assigns_by_threshold():
    # 阈值定级：High 70 ≥ 60 → Must Read；Genomics 50 ≥ 20 → Important；Low 19 → Reference
    today = date(2026, 7, 22)
    d = today.isoformat()
    items = [
        {"paper": _paper("Low paper", journal="Obscure", date_str=d), "analysis": {}},
        {"paper": _paper("High paper", journal="Nature", date_str=d), "analysis": {}},
        {"paper": _paper("Genomics paper", journal="Nature", date_str=d), "analysis": {}},
    ]
    ranked = rank_items(items, USER, FakeLLM(), JOURNAL_TIERS, WEIGHTS, THRESHOLDS, today)
    assert [it["paper"].title for it in ranked] == ["High paper", "Genomics paper", "Low paper"]
    assert [it["category"] for it in ranked] == ["Must Read", "Important", "Reference"]
    assert ranked[0]["score"] > ranked[1]["score"] > ranked[2]["score"]
    assert ranked[0]["reason"] == "高相关"


def test_rank_items_all_low_scores_no_must_read():
    # 宁缺毋滥：当日全部低分时没有 Must Read / Important，不凑配额
    today = date(2026, 7, 22)
    d = today.isoformat()
    items = [{"paper": _paper(f"Low paper {i}", journal="Obscure", date_str=d), "analysis": {}}
             for i in range(3)]
    ranked = rank_items(items, USER, FakeLLM(), JOURNAL_TIERS, WEIGHTS, THRESHOLDS, today)
    assert [it["category"] for it in ranked] == ["Reference"] * 3


def test_rank_items_llm_failure_keeps_pipeline_alive():
    class BadLLM:
        def complete(self, prompt):
            return "```json\n{broken\n```"

    items = [{"paper": _paper("Genomics paper"), "analysis": {}}]
    ranked = rank_items(items, USER, BadLLM(), {}, WEIGHTS, THRESHOLDS)
    # LLM 两个维度回退 50 分，流程不中断且仍产出分数与定级（39 分 → Important）
    assert ranked[0]["score"] == 39
    assert ranked[0]["reason"] == ""
    assert ranked[0]["category"] == "Important"


def test_load_ranker_weights_defaults_and_override(tmp_path):
    assert load_ranker_weights(tmp_path / "missing.yaml") == WEIGHTS  # 文件缺失用默认
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.dump({"ranker": {"weights": {"personal": 50}}}), encoding="utf-8")
    weights = load_ranker_weights(path)
    assert weights["personal"] == 50
    assert weights["lab"] == 20  # 未覆盖字段回退默认


def test_load_ranker_thresholds_defaults_and_override(tmp_path):
    assert load_ranker_thresholds(tmp_path / "missing.yaml") == {"must_read": 75, "important": 60}
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.dump({"ranker": {"thresholds": {"must_read": 80}}}), encoding="utf-8")
    thresholds = load_ranker_thresholds(path)
    assert thresholds["must_read"] == 80
    assert thresholds["important"] == 60  # 未覆盖字段回退默认
