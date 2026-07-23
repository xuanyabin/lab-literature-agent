import yaml

from recommendation.scorer import (
    assign_categories,
    journal_fallback,
    load_journal_tiers,
    load_scoring_config,
    rank_papers,
    score_paper,
)
from sources.paper import Paper

USER = {
    "research_interest": ["insect evolution", "hormone"],
    "keywords": [],
    "methods": ["single-cell RNA sequencing"],
    "species": ["honeybee", "Bombus"],
    "exclude": ["cancer"],
}

CONFIG = {
    "weights": {"species": 3, "methods": 2, "research_interest": 1, "keywords": 1},
    "title_bonus": 1,
    "frequency_bonus": 1,
    "frequency_cap": 3,
    "journal_t0": 5,
    "journal_t1": 2,
    "tiers": {"must_read": 3, "important": 5},
    "journal_tiers": {},
}


def _paper(title, abstract="", keywords=None, journal=""):
    return Paper(title=title, abstract=abstract, authors="", journal=journal,
                 date="", doi="", url="", keywords=keywords or [])


def test_score_accumulates_per_category():
    p = _paper("Honeybee brain atlas", abstract="insect evolution study",
               keywords=["single-cell RNA sequencing"])
    # species 3 + 标题命中 1 + research_interest 1 + methods 2
    assert score_paper(p, USER, CONFIG) == 7


def test_term_weight_counted_once_frequency_adds_bonus():
    p = _paper("Honeybee honeybee honeybee")
    # species 3（同一词权重只计一次）+ 标题 1 + 频次 2×1（共 3 次命中）
    assert score_paper(p, USER, CONFIG) == 6


def test_frequency_bonus_capped():
    cfg = {**CONFIG, "frequency_cap": 2}
    p = _paper("Unrelated title", abstract="hormone " * 10)
    # research_interest 1 + 频次 2（封顶 frequency_cap）
    assert score_paper(p, USER, cfg) == 3


def test_title_bonus_only_when_term_in_title():
    assert score_paper(_paper("Bombus atlas"), USER, CONFIG) == 4  # 3 + 标题 1
    assert score_paper(_paper("Brain atlas", abstract="A Bombus study"), USER, CONFIG) == 3


def test_score_matches_abstract_and_keywords_not_just_title():
    p = _paper("Unrelated title", abstract="A Bombus study", keywords=["hormone"])
    assert score_paper(p, USER, CONFIG) == 4


def test_journal_tier_bonus():
    cfg = {**CONFIG, "journal_tiers": {"nature": "t0", "plos one": "t1"}}
    assert score_paper(_paper("Honeybee study", journal="Nature"), USER, cfg) == 3 + 1 + 5
    assert score_paper(_paper("Honeybee study", journal="PLOS ONE"), USER, cfg) == 3 + 1 + 2
    # 未分层期刊不加分
    assert score_paper(_paper("Honeybee study", journal="Some Obscure Journal"), USER, cfg) == 3 + 1


def test_journal_name_normalized():
    # journal_tiers 的键是规范化后的刊名（load_journal_tiers 负责规范化）
    cfg = {**CONFIG, "journal_tiers": {"science": "t0", "nature ecology evolution": "t0"}}
    # 括号附加说明、& 符号、大小写差异不影响匹配
    assert score_paper(_paper("x", journal="Science (New York, N.Y.)"), USER, cfg) == 5
    assert score_paper(_paper("x", journal="Nature Ecology & Evolution"), USER, cfg) == 5


def test_rank_papers_sorts_desc_and_drops_exclude():
    low = _paper("Bone health survey")
    mid = _paper("Insect evolution review")
    high = _paper("Honeybee brain", abstract="single-cell RNA sequencing")
    excluded = _paper("Cancer single-cell RNA sequencing in honeybee")
    ranked = rank_papers([low, mid, high, excluded], USER, CONFIG)
    assert [p.title for _, p in ranked] == [high.title, mid.title, low.title]
    assert ranked[0][0] > ranked[1][0] > ranked[2][0] == 0
    assert excluded not in [p for _, p in ranked]


def test_load_scoring_config_defaults(tmp_path):
    # 配置文件缺失字段时回退默认值
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.dump({"weights": {"species": 5}}), encoding="utf-8")
    cfg = load_scoring_config(path, journals_path=tmp_path / "missing.yaml")
    assert cfg["weights"]["species"] == 5
    assert cfg["weights"]["methods"] == 2
    assert cfg["weights"]["research_interest"] == 1
    assert cfg["title_bonus"] == 1
    assert cfg["tiers"] == {"must_read": 3, "important": 5}
    assert cfg["journal_tiers"] == {}


USER_WITH_ALIASES = {
    **USER,
    "aliases": {"honeybee": ["Apis mellifera", "Apis"]},
}


def test_alias_hit_scores_like_original_term():
    # 摘要只出现别名 "Apis mellifera"，应视同 species 词 honeybee 命中
    p = _paper("Brain atlas", abstract="An Apis mellifera study")
    assert score_paper(p, USER_WITH_ALIASES, CONFIG) == 3
    assert score_paper(p, USER, CONFIG) == 0  # 无 aliases 时不命中


def test_alias_and_original_weight_counted_once():
    # 原词与别名同时命中，同一原词只计一次权重
    p = _paper("Honeybee brain", abstract="also Apis mellifera")
    assert score_paper(p, USER_WITH_ALIASES, CONFIG) == 4  # 3 + 标题 1


def test_alias_matching_is_case_insensitive():
    p = _paper("APIS MELLIFERA genome")
    assert score_paper(p, USER_WITH_ALIASES, CONFIG) == 4  # 3 + 标题 1


def test_load_journal_tiers_roundtrip(tmp_path):
    path = tmp_path / "journals.yaml"
    path.write_text(yaml.dump({"t0": ["Nature"], "t1": ["PLOS ONE"]}), encoding="utf-8")
    assert load_journal_tiers(path) == {"nature": "t0", "plos one": "t1"}


def test_load_journal_tiers_missing_file(tmp_path):
    assert load_journal_tiers(tmp_path / "missing.yaml") == {}


def test_assign_categories_quota():
    ranked = [(5, _paper(f"p{i}")) for i in range(6)]
    out = assign_categories(ranked, {"must_read": 2, "important": 2})
    assert [c for _, c, _ in out] == ["Must Read", "Must Read", "Important", "Important",
                                      "Reference", "Reference"]


def test_assign_categories_zero_score_never_must_read():
    ranked = [(0, _paper("a")), (0, _paper("b"))]
    out = assign_categories(ranked, {"must_read": 3, "important": 5})
    assert [c for _, c, _ in out] == ["Reference", "Reference"]


def test_assign_categories_short_list_fills_in_order():
    ranked = [(9, _paper("a")), (8, _paper("b"))]
    out = assign_categories(ranked, {"must_read": 3, "important": 5})
    assert [c for _, c, _ in out] == ["Must Read", "Must Read"]


def test_learned_terms_add_score():
    # 反馈学习词表命中：按有效权重加分（max(1, round(eff))），与手配词表分离
    user = {**USER, "learned_terms": [("microbiome", 1.0)]}
    p = _paper("Unrelated title", abstract="A microbiome study")
    assert score_paper(p, user, CONFIG) == 1
    assert score_paper(p, USER, CONFIG) == 0  # 未注入学习词的用户不受影响


def test_learned_term_title_bonus():
    user = {**USER, "learned_terms": [("microbiome", 1.0)]}
    assert score_paper(_paper("Gut microbiome atlas"), user, CONFIG) == 2  # 1 + 标题 1


def test_learned_score_capped_by_default():
    # CONFIG 未配 learned_score_cap，回退默认值 6
    user = {**USER, "learned_terms": [("t1x", 3.0), ("t2x", 3.0), ("t3x", 3.0)]}
    p = _paper("Unrelated", abstract="t1x t2x t3x")
    assert score_paper(p, user, CONFIG) == 6  # 3 词各 3 分共 9，封顶 6


def test_learned_score_cap_from_config():
    user = {**USER, "learned_terms": [("t1x", 3.0), ("t2x", 3.0)]}
    cfg = {**CONFIG, "learned_score_cap": 4}
    p = _paper("Unrelated", abstract="t1x t2x")
    assert score_paper(p, user, cfg) == 4


def test_lab_topics_score_like_any_weighted_field():
    # lab_topics 由 main.apply_lab_profile 注入 user dict，scorer 通用遍历 weights 即可命中
    cfg = {**CONFIG, "weights": {**CONFIG["weights"], "lab_topics": 1}}
    user = {**USER, "lab_topics": ["genomics"]}
    p = _paper("Unrelated title", abstract="A genomics study")
    assert score_paper(p, user, cfg) == 1
    assert score_paper(p, USER, cfg) == 0  # 未注入 lab_topics 的用户不受影响


def test_journal_fallback_not_triggered_when_strong_enough():
    cfg = {**CONFIG, "journal_tiers": {"nature": "t0"}}
    ranked = [
        (10, _paper("s0")), (9, _paper("s1")), (8, _paper("s2")),
        (5, _paper("nature extra", journal="Nature")),
    ]
    out = journal_fallback(ranked, cfg, limit=3)
    assert [p.title for _, p in out] == ["s0", "s1", "s2"]


def test_journal_fallback_replaces_weak_tail_with_tiered():
    cfg = {**CONFIG, "journal_tiers": {"nature": "t0", "cell": "t0"}}
    ranked = [
        (9, _paper("strong0")),
        (2, _paper("weak1")),
        (1, _paper("weak2")),
        (0, _paper("weak3")),
        (5, _paper("nature a", journal="Nature")),
        (5, _paper("cell b", journal="Cell")),
        (0, _paper("obscure", journal="Obscure")),
    ]
    out = journal_fallback(ranked, cfg, limit=4)
    titles = [p.title for _, p in out]
    # 强相关仅 strong0 一篇，缺口 must_read-1=2：尾部最弱的两篇非分层论文被顶刊递补
    assert len(out) == 4
    assert titles[0] == "strong0"
    assert "nature a" in titles and "cell b" in titles
    assert "weak1" in titles  # 只递补缺口数，其余弱相关保留
    assert "weak2" not in titles and "weak3" not in titles
    assert "obscure" not in titles  # 未分层期刊不参与递补
    assert [s for s, _ in out] == sorted((s for s, _ in out), reverse=True)


def test_journal_fallback_noop_when_within_limit():
    cfg = {**CONFIG, "journal_tiers": {"nature": "t0"}}
    ranked = [(1, _paper("weak0")), (0, _paper("weak1"))]
    out = journal_fallback(ranked, cfg, limit=15)
    assert [p.title for _, p in out] == ["weak0", "weak1"]


def test_journal_fallback_noop_without_tiered_pool():
    ranked = [(2, _paper(f"w{i}")) for i in range(5)]
    out = journal_fallback(ranked, CONFIG, limit=3)  # CONFIG 无分层名单
    assert [p.title for _, p in out] == ["w0", "w1", "w2"]
