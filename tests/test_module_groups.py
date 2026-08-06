"""模块归属（processing/module_groups.py）测试：主归属规则与显示名回退。"""

from processing.module_groups import OTHER, assign_module, group_label


GROUPS = {
    "spatial": ["spatial omics", "MERFISH"],
    "genomics": ["comparative genomics", "phylogenomics"],
    "ants": ["ants", "eusociality"],
}


def test_single_hit_group_wins():
    text = "a study using spatial omics to map tissues"
    assert assign_module(text, GROUPS, subscribed=[]) == "spatial"


def test_most_hits_wins_over_subscribed():
    # spatial 命中 2 词，genomics 命中 1 词：即使订阅了 genomics 也取 spatial
    text = "spatial omics and MERFISH meet comparative genomics"
    assert assign_module(text, GROUPS, subscribed=["genomics"]) == "spatial"


def test_tie_prefers_subscribed_group():
    text = "spatial omics meets comparative genomics"  # 各命中 1 词
    assert assign_module(text, GROUPS, subscribed=["genomics"]) == "genomics"
    # 未订阅时按组序取前者
    assert assign_module(text, GROUPS, subscribed=[]) == "spatial"


def test_no_hit_returns_other():
    assert assign_module("quantum computing survey", GROUPS, subscribed=[]) == OTHER


def test_empty_groups_returns_other():
    assert assign_module("anything", {}, subscribed=["x"]) == OTHER
    assert assign_module("anything", None, subscribed=[]) == OTHER


def test_aliases_participate():
    # ants 的 alias "social insects" 命中应计入 ants 组
    text = "the rise of social insects in evolution"
    groups = {"ants": ["ants"]}
    assert assign_module(text, groups, subscribed=[],
                         aliases={"ants": ["social insects"]}) == "ants"


def test_matching_is_boundary_aware():
    # "ant" 不应误命中 "plants"
    assert assign_module("study of plants", {"g": ["ant"]}, []) == OTHER


def test_group_label_fallback():
    labels = {"spatial": "空间组学"}
    assert group_label(labels, "spatial") == "空间组学"
    assert group_label(labels, "unknown") == "unknown"
    assert group_label(None, "unknown") == "unknown"
    assert group_label(labels, OTHER) == OTHER
