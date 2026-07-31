import yaml

from main import (
    _artifact_union,
    _candidate_limit,
    _personal_title_fallback,
    apply_lab_profile,
    load_users,
)
from sources.paper import Paper


def _write_user(dir_path, filename, **fields):
    (dir_path / filename).write_text(yaml.safe_dump(fields, allow_unicode=True), encoding="utf-8")


def test_load_users_skips_inactive_and_sorts(tmp_path):
    _write_user(tmp_path, "user002.yaml", name="B", email="b@x.com", active=True)
    _write_user(tmp_path, "user001.yaml", name="A", email="a@x.com")  # 缺省视为 active
    _write_user(tmp_path, "user003.yaml", name="C", email="c@x.com", active=False)
    users = load_users(tmp_path)
    assert [slug for slug, _ in users] == ["user001", "user002"]
    assert users[0][1]["email"] == "a@x.com"


def test_load_users_empty_dir(tmp_path):
    assert load_users(tmp_path) == []


def test_candidate_limit_defaults_to_config_and_is_separate_from_email_limit():
    assert _candidate_limit({"candidate_limit_per_user": 30}, final_limit=15) == 30
    assert _candidate_limit({}, final_limit=15) == 15
    assert _candidate_limit({"candidate_limit_per_user": 0}, final_limit=15) == 1
    assert _candidate_limit({"candidate_limit_per_user": "bad"}, final_limit=15) == 15


def test_apply_lab_profile_v5_layers():
    # V5：global_core 全员召回 + 订阅 topic_groups 展开；rank_only 只进 lab_topics
    user = {"name": "A", "email": "a@x.com", "topic_groups": ["social_insects"],
            "aliases": {"JH3": ["juvenile hormone III"]}}
    lab = {"global_core": ["evolution", "genomics"],
           "topic_groups": {"social_insects": ["eusociality", "fire ant"],
                            "brain_evolution": ["pallium"]},
           "rank_only": ["spatial transcriptomics"],
           "noise_terms": ["cancer"],
           "aliases": {"JH3": ["lab variant"], "scRNA-seq": ["single-cell RNA sequencing"]}}
    merged = apply_lab_profile(user, lab)
    assert merged["lab_recall"] == ["evolution", "genomics", "eusociality", "fire ant"]
    assert merged["lab_topics"] == ["evolution", "genomics", "eusociality", "fire ant",
                                    "spatial transcriptomics"]
    assert merged["noise_terms"] == ["cancer"]
    # 个人别名优先于实验室别名；实验室独有的别名保留
    assert merged["aliases"] == {"JH3": ["juvenile hormone III"],
                                 "scRNA-seq": ["single-cell RNA sequencing"]}
    assert "lab_recall" not in user  # 原 dict 不被修改


def test_apply_lab_profile_unknown_topic_group_ignored():
    user = {"name": "A", "email": "a@x.com", "topic_groups": ["no_such_group"]}
    lab = {"global_core": ["evolution"], "topic_groups": {"social_insects": ["eusociality"]}}
    merged = apply_lab_profile(user, lab)
    assert merged["lab_recall"] == ["evolution"]


def test_apply_lab_profile_legacy_topics_key():
    # 旧版 topics 键按 global_core 处理（向后兼容）
    user = {"name": "A", "email": "a@x.com"}
    merged = apply_lab_profile(user, {"topics": ["genomics"]})
    assert merged["lab_recall"] == ["genomics"]
    assert merged["lab_topics"] == ["genomics"]
    assert merged["noise_terms"] == []


def test_apply_lab_profile_with_empty_lab():
    user = {"name": "A", "email": "a@x.com"}
    merged = apply_lab_profile(user, {})
    assert merged["lab_recall"] == []
    assert merged["lab_topics"] == []
    assert merged["noise_terms"] == []
    assert merged["aliases"] == {}


def test_apply_lab_profile_default_groups_auto_subscribed():
    # default_groups 全员自动订阅，无需个人 yaml 声明
    user = {"name": "A", "email": "a@x.com"}
    lab = {"default_groups": ["core_spatial_omics"],
           "topic_groups": {"core_spatial_omics": ["MERFISH", "spatial omics"],
                            "social_insects": ["eusociality"]},
           "rank_only": ["aging"]}
    merged = apply_lab_profile(user, lab)
    assert merged["lab_recall"] == ["MERFISH", "spatial omics"]
    assert merged["lab_topics"] == ["MERFISH", "spatial omics", "aging"]


def test_apply_lab_profile_duplicate_subscription_deduped():
    # 用户重复订阅 default 组时按词去重，不双计打分
    user = {"name": "A", "email": "a@x.com",
            "topic_groups": ["core_spatial_omics", "social_insects"]}
    lab = {"default_groups": ["core_spatial_omics"],
           "topic_groups": {"core_spatial_omics": ["MERFISH", "Spatial Omics"],
                            "social_insects": ["eusociality", "merfish"]},
           "rank_only": ["MERFISH"]}
    merged = apply_lab_profile(user, lab)
    assert merged["lab_recall"] == ["MERFISH", "Spatial Omics", "eusociality"]
    assert merged["lab_topics"] == ["MERFISH", "Spatial Omics", "eusociality"]


# ---------- 个人关键词强命中兜底（V5） ----------

def _paper(doi, title):
    return Paper(title=title, abstract="", authors="", journal="J",
                 date="", doi=doi, url="", keywords=[])


def _paper_with_abstract(doi, title, abstract):
    return Paper(title=title, abstract=abstract, authors="", journal="J",
                 date="", doi=doi, url="", keywords=[])


def test_personal_title_fallback_adds_title_hits_beyond_shortlist():
    user = {"species": ["fire ant"], "keywords": [], "research_interest": [],
            "methods": [], "aliases": {"fire ant": ["Solenopsis invicta"]}}
    fresh = [(5, _paper("10.1/a", "Unrelated high score")),
             (4, _paper("10.1/b", "Another unrelated")),
             (1, _paper("10.1/c", "A Solenopsis invicta genome study")),
             (0, _paper("10.1/d", "Nothing matches"))]
    shortlist = fresh[:2]
    extras = _personal_title_fallback(fresh, shortlist, user, max_extra=5)
    assert [p.doi for _, p in extras] == ["10.1/c"]  # 别名命中标题被兜底


def test_personal_title_fallback_skips_already_picked_and_caps():
    user = {"species": ["fire ant"], "keywords": [], "research_interest": [],
            "methods": [], "aliases": {}}
    fresh = [(5, _paper("10.1/a", "Fire ant already in shortlist")),
             (1, _paper("10.1/c", "Fire ant extra one")),
             (1, _paper("10.1/d", "Fire ant extra two")),
             (1, _paper("10.1/e", "Fire ant extra three"))]
    shortlist = fresh[:1]
    extras = _personal_title_fallback(fresh, shortlist, user, max_extra=2)
    assert [p.doi for _, p in extras] == ["10.1/c", "10.1/d"]  # 不重复 + 上限


def test_personal_fallback_adds_two_personal_abstract_hits():
    user = {"species": [], "keywords": ["centromere", "karyotype evolution"],
            "research_interest": [], "methods": [], "aliases": {}}
    fresh = [(5, _paper("10.1/a", "Unrelated high score")),
             (1, _paper_with_abstract("10.1/b", "Chromosome inheritance",
                                      "CRISPR centromere fission reveals karyotype evolution."))]
    extras = _personal_title_fallback(fresh, fresh[:1], user, max_extra=5)
    assert [p.doi for _, p in extras] == ["10.1/b"]


def test_personal_title_fallback_disabled_or_no_terms():
    fresh = [(1, _paper("10.1/a", "Fire ant"))]
    assert _personal_title_fallback(fresh, [], {"species": ["fire ant"]}, 0) == []
    assert _personal_title_fallback(fresh, [], {"species": []}, 5) == []


def test_artifact_union_dedupes_same_paper_across_users():
    same_a = _paper("10.1/a", "Shared paper")
    same_b = _paper("10.1/a", "Shared paper duplicate")
    unique = _paper("10.1/b", "Unique paper")
    union = _artifact_union({
        "user001": [(3, same_a), (2, unique)],
        "user002": [(4, same_b)],
    })
    assert [p.doi for p in union] == ["10.1/a", "10.1/b"]
