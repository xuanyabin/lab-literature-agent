from datetime import date, timedelta

import yaml

from recommendation.ranker import (
    _parse_judgments,
    final_score,
    journal_influence,
    judge_paper,
    lab_relevance,
    load_ranker_batch_size,
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

WEIGHTS = {"personal": 30, "lab": 20, "journal": 30, "novelty": 10, "method": 10, "recency": 0}
THRESHOLDS = {"must_read": 60, "important": 20}
JOURNAL_TIERS = {"nature": "t0", "plos one": "t1"}


class FakeLLM:
    """批处理感知的假 LLM：解析 prompt 中的"标题："行，逐篇返回评分数组。"""

    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str) -> str:
        import json
        self.calls += 1
        titles = [line.split("：", 1)[1] for line in prompt.splitlines()
                  if line.startswith("标题：")]
        out = [
            {"personal_relevance": 100 if "High" in t else 0,
             "novelty": 100 if "High" in t else 0,
             "reason": "高相关" if "High" in t else "低相关"}
            for t in titles
        ]
        return json.dumps(out, ensure_ascii=False)


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
    assert final_score(dims, WEIGHTS) == 70  # 只扣掉 personal 的 30%


def test_judge_paper_fallback_on_bad_json():
    class BadLLM:
        def complete(self, prompt):
            return "not json"

    result = judge_paper(_paper("x"), {}, USER, BadLLM())
    assert result == {"personal_relevance": 50, "novelty": 50, "reason": ""}


def test_rank_items_sorts_by_final_score_and_assigns_by_threshold():
    # 阈值定级：High 70 ≥ 60 → Must Read；Genomics 40 ≥ 20 → Important；Low 9 → Reference
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


def test_rank_items_push_floor_filters_low_scores():
    # 推送下限：低分小刊（9 分）被过滤，不相关顶刊（30 分）保留露面
    today = date(2026, 7, 22)
    d = today.isoformat()
    thresholds = {**THRESHOLDS, "push_floor": 30}
    items = [
        {"paper": _paper("Low paper", journal="Obscure", date_str=d), "analysis": {}},
        {"paper": _paper("Low paper in Nature", journal="Nature", date_str=d), "analysis": {}},
    ]
    ranked = rank_items(items, USER, FakeLLM(), JOURNAL_TIERS, WEIGHTS, thresholds, today)
    assert [it["paper"].title for it in ranked] == ["Low paper in Nature"]
    assert ranked[0]["score"] == 30


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


def test_load_ranker_batch_size_defaults_and_override(tmp_path):
    assert load_ranker_batch_size(tmp_path / "missing.yaml") == 5  # 文件缺失用默认
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.dump({"ranker": {"batch_size": 3}}), encoding="utf-8")
    assert load_ranker_batch_size(path) == 3
    path.write_text(yaml.dump({"ranker": {"batch_size": 0}}), encoding="utf-8")
    assert load_ranker_batch_size(path) == 1  # 下限 1
    path.write_text(yaml.dump({"ranker": {"batch_size": "abc"}}), encoding="utf-8")
    assert load_ranker_batch_size(path) == 5  # 非法值回退默认


def test_parse_judgments_valid_array():
    raw = '[{"personal_relevance": 85, "novelty": 70, "reason": "r1"},' \
          ' {"personal_relevance": 10, "novelty": 20, "reason": "r2"}]'
    out = _parse_judgments(raw, 2)
    assert out[0] == {"personal_relevance": 85, "novelty": 70, "reason": "r1"}
    assert out[1]["personal_relevance"] == 10


def test_parse_judgments_item_fallback_and_clamp():
    # 单条非法只影响该条；合法条照常解析且分数裁剪到 0-100
    raw = '[{"personal_relevance": 250, "novelty": -5, "reason": "r"}, {"bad": 1}]'
    out = _parse_judgments(raw, 2)
    assert out[0] == {"personal_relevance": 100, "novelty": 0, "reason": "r"}
    assert out[1] == {"personal_relevance": 50, "novelty": 50, "reason": ""}


def test_parse_judgments_invalid_or_length_mismatch_all_neutral():
    neutral = {"personal_relevance": 50, "novelty": 50, "reason": ""}
    assert _parse_judgments("not json", 2) == [neutral, neutral]
    assert _parse_judgments('[{"personal_relevance": 1, "novelty": 1}]', 2) == [neutral, neutral]
    # 单个对象在 n=1 时可接受
    assert _parse_judgments('{"personal_relevance": 88, "novelty": 66, "reason": "x"}', 1) == \
        [{"personal_relevance": 88, "novelty": 66, "reason": "x"}]


def test_rank_items_batches_llm_calls():
    # 3 篇按 batch_size=2 分 2 批：LLM 只调 2 次，分数与定级不变
    today = date(2026, 7, 22)
    d = today.isoformat()
    items = [
        {"paper": _paper("Low paper", journal="Obscure", date_str=d), "analysis": {}},
        {"paper": _paper("High paper", journal="Nature", date_str=d), "analysis": {}},
        {"paper": _paper("Genomics paper", journal="Nature", date_str=d), "analysis": {}},
    ]
    llm = FakeLLM()
    ranked = rank_items(items, USER, llm, JOURNAL_TIERS, WEIGHTS, THRESHOLDS, today,
                        batch_size=2, max_workers=1)
    assert llm.calls == 2
    assert [it["paper"].title for it in ranked] == ["High paper", "Genomics paper", "Low paper"]
    assert ranked[0]["reason"] == "高相关"


def test_rank_items_batch_failure_falls_back_per_batch():
    # 一批输出非法时整批回退中性分，其余批不受影响
    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt):
            self.calls += 1
            if "High paper" in prompt:
                return '{"personal_relevance": 100, "novelty": 100, "reason": "高相关"}'
            return "not json"

    today = date(2026, 7, 22)
    d = today.isoformat()
    items = [
        {"paper": _paper("Low paper", journal="Obscure", date_str=d), "analysis": {}},
        {"paper": _paper("High paper", journal="Nature", date_str=d), "analysis": {}},
    ]
    ranked = rank_items(items, USER, FlakyLLM(), JOURNAL_TIERS, WEIGHTS, THRESHOLDS, today,
                        batch_size=1, max_workers=1)
    assert ranked[0]["paper"].title == "High paper"  # 合法批正常
    # 非法批回退中性分（personal/novelty 各 50）：30*0.3+50*0.3+50*0.1+0*0.2+0*0.1=29
    assert ranked[1]["score"] == 29
