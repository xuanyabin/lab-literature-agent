from recommendation.scorer import load_scoring_config, rank_papers, score_paper
from sources.paper import Paper

USER = {
    "research_interest": ["insect evolution", "hormone"],
    "keywords": [],
    "methods": ["single-cell RNA sequencing"],
    "species": ["honeybee", "Bombus"],
    "exclude": ["cancer"],
}

WEIGHTS = {"species": 3, "methods": 2, "research_interest": 1, "keywords": 1}
CONFIG = {"weights": WEIGHTS}


def _paper(title, abstract="", keywords=None):
    return Paper(title=title, abstract=abstract, authors="", journal="",
                 date="", doi="", url="", keywords=keywords or [])


def test_score_accumulates_per_category():
    p = _paper("Honeybee brain atlas", abstract="insect evolution study",
               keywords=["single-cell RNA sequencing"])
    # species +3, methods +2, research_interest +1
    assert score_paper(p, USER, WEIGHTS) == 6


def test_score_counts_each_term_once():
    p = _paper("Honeybee honeybee honeybee")
    assert score_paper(p, USER, WEIGHTS) == 3


def test_score_matches_abstract_and_keywords_not_just_title():
    p = _paper("Unrelated title", abstract="A Bombus study", keywords=["hormone"])
    assert score_paper(p, USER, WEIGHTS) == 4


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
    # 配置文件缺失字段时回退默认权重
    import yaml
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.dump({"weights": {"species": 5}}), encoding="utf-8")
    cfg = load_scoring_config(path)
    assert cfg["weights"]["species"] == 5
    assert cfg["weights"]["methods"] == 2
    assert cfg["weights"]["research_interest"] == 1


USER_WITH_ALIASES = {
    **USER,
    "aliases": {"honeybee": ["Apis mellifera", "Apis"]},
}


def test_alias_hit_scores_like_original_term():
    # 摘要只出现别名 "Apis mellifera"，应视同 species 词 honeybee 命中
    p = _paper("Brain atlas", abstract="An Apis mellifera study")
    assert score_paper(p, USER_WITH_ALIASES, WEIGHTS) == 3
    assert score_paper(p, USER, WEIGHTS) == 0  # 无 aliases 时不命中


def test_alias_and_original_hit_counted_once():
    # 原词与别名同时命中，同一原词只计一次权重
    p = _paper("Honeybee brain", abstract="also Apis mellifera")
    assert score_paper(p, USER_WITH_ALIASES, WEIGHTS) == 3


def test_alias_matching_is_case_insensitive():
    p = _paper("APIS MELLIFERA genome")
    assert score_paper(p, USER_WITH_ALIASES, WEIGHTS) == 3
