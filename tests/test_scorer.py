import yaml

from recommendation.scorer import (
    _normalize_journal,
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
    "weights": {"species": 1, "methods": 1, "research_interest": 1,
                "keywords": 1, "lab_topics": 1},
    "title_bonus": 1,
    "frequency_bonus": 1,
    "frequency_cap": 3,
    "journal_tiers": {},
}


def _paper(title, abstract="", keywords=None, journal=""):
    return Paper(title=title, abstract=abstract, authors="", journal=journal,
                 date="", doi="", url="", keywords=keywords or [])


def test_score_accumulates_per_hit_equal_weight():
    # 关键词等权：species 1 + 标题命中 1 + research_interest 1 + methods 1
    p = _paper("Honeybee brain atlas", abstract="insect evolution study",
               keywords=["single-cell RNA sequencing"])
    assert score_paper(p, USER, CONFIG) == 4


def test_term_weight_counted_once_frequency_adds_bonus():
    p = _paper("Honeybee honeybee honeybee")
    # species 1（同一词权重只计一次）+ 标题 1 + 频次 2×1（共 3 次命中）
    assert score_paper(p, USER, CONFIG) == 4


def test_frequency_bonus_capped():
    cfg = {**CONFIG, "frequency_cap": 2}
    p = _paper("Unrelated title", abstract="hormone " * 10)
    # research_interest 1 + 频次 2（封顶 frequency_cap）
    assert score_paper(p, USER, cfg) == 3


def test_title_bonus_only_when_term_in_title():
    assert score_paper(_paper("Bombus atlas"), USER, CONFIG) == 2  # 1 + 标题 1
    assert score_paper(_paper("Brain atlas", abstract="A Bombus study"), USER, CONFIG) == 1


def test_score_matches_abstract_and_keywords_not_just_title():
    p = _paper("Unrelated title", abstract="A Bombus study", keywords=["hormone"])
    assert score_paper(p, USER, CONFIG) == 2


def test_journal_does_not_affect_coarse_score():
    # 粗筛去期刊化：期刊分层不影响粗筛分数（期刊因素只在精排 journal 维度体现）
    assert score_paper(_paper("Honeybee study", journal="Nature"), USER, CONFIG) == \
        score_paper(_paper("Honeybee study", journal="Some Obscure Journal"), USER, CONFIG)


def test_journal_name_normalized():
    # 刊名规范化：去括号附加说明、忽略标点与大小写（精排 journal 维度依赖）
    assert _normalize_journal("Science (New York, N.Y.)") == "science"
    assert _normalize_journal("Nature Ecology & Evolution") == "nature ecology evolution"


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
    assert cfg["weights"]["methods"] == 1
    assert cfg["weights"]["research_interest"] == 1
    assert cfg["title_bonus"] == 1
    assert cfg["journal_tiers"] == {}
    # 粗筛去期刊化后，配置不再包含期刊加分与定级配额
    assert "journal_t0" not in cfg and "tiers" not in cfg


USER_WITH_ALIASES = {
    **USER,
    "aliases": {"honeybee": ["Apis mellifera", "Apis"]},
}


def test_alias_hit_scores_like_original_term():
    # 摘要只出现别名 "Apis mellifera"，应视同 species 词 honeybee 命中
    p = _paper("Brain atlas", abstract="An Apis mellifera study")
    assert score_paper(p, USER_WITH_ALIASES, CONFIG) == 1
    assert score_paper(p, USER, CONFIG) == 0  # 无 aliases 时不命中


def test_alias_and_original_weight_counted_once():
    # 原词与别名同时命中，同一原词只计一次权重
    p = _paper("Honeybee brain", abstract="also Apis mellifera")
    assert score_paper(p, USER_WITH_ALIASES, CONFIG) == 2  # 1 + 标题 1


def test_alias_matching_is_case_insensitive():
    p = _paper("APIS MELLIFERA genome")
    assert score_paper(p, USER_WITH_ALIASES, CONFIG) == 2  # 1 + 标题 1


def test_load_journal_tiers_roundtrip(tmp_path):
    path = tmp_path / "journals.yaml"
    path.write_text(yaml.dump({"t0": ["Nature"], "t1": ["PLOS ONE"]}), encoding="utf-8")
    assert load_journal_tiers(path) == {"nature": "t0", "plos one": "t1"}


def test_load_journal_tiers_missing_file(tmp_path):
    assert load_journal_tiers(tmp_path / "missing.yaml") == {}


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
    # lab_topics 由 main.apply_lab_profile 注入 user dict，与其他词等权命中
    user = {**USER, "lab_topics": ["genomics"]}
    p = _paper("Unrelated title", abstract="A genomics study")
    assert score_paper(p, user, CONFIG) == 1
    assert score_paper(p, USER, CONFIG) == 0  # 未注入 lab_topics 的用户不受影响
