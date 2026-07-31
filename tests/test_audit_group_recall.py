"""scripts/audit_group_recall.py 的单元测试（count_fn 注入 mock，不连 PubMed）。"""

import pytest

from scripts.audit_group_recall import DEAD, HOT, audit_groups, classify, render


def test_classify_boundaries():
    assert classify(0, 150) == DEAD
    assert classify(1, 150) == ""
    assert classify(150, 150) == ""
    assert classify(151, 150) == HOT


def _lab():
    return {
        "topic_groups": {
            "core_a": ["alive term", "dead term", "hot term"],
            "core_b": ["ok term"],
        },
        "rank_only": ["noisy term"],
    }


def test_audit_groups_flags_and_rank_only_pseudo_group():
    counts = {"alive term": 10, "dead term": 0, "hot term": 999,
              "ok term": 5, "noisy term": 500}
    results = audit_groups(_lab(), days=90, hot=150,
                           count_fn=lambda term, days: counts[term])
    assert results["core_a"] == [("alive term", 10, ""),
                                 ("dead term", 0, DEAD),
                                 ("hot term", 999, HOT)]
    assert results["core_b"] == [("ok term", 5, "")]
    assert results["rank_only"] == [("noisy term", 500, HOT)]


def test_audit_groups_only_group_filter():
    counts = {"alive term": 10, "dead term": 0, "hot term": 999}
    results = audit_groups(_lab(), days=90, hot=150,
                           count_fn=lambda term, days: counts[term],
                           only_group="core_a")
    assert list(results) == ["core_a"]


def test_audit_groups_unknown_group_raises():
    with pytest.raises(ValueError, match="未知分组"):
        audit_groups(_lab(), days=90, hot=150,
                     count_fn=lambda term, days: 0, only_group="nope")


def test_render_summary_lists_dead_and_hot():
    results = {"core_a": [("dead term", 0, DEAD), ("hot term", 999, HOT),
                          ("ok term", 3, "")]}
    text = render(results, days=90, hot=150)
    assert "DEAD" in text and "dead term" in text
    assert "HOT" in text and "hot term" in text
    assert "ok term" in text
