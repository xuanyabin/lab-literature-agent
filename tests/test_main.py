import yaml

from main import apply_lab_profile, load_users


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


def test_apply_lab_profile_injects_topics_and_merges_aliases():
    user = {"name": "A", "email": "a@x.com", "aliases": {"JH3": ["juvenile hormone III"]}}
    lab = {"topics": ["genomics", "single-cell"],
           "aliases": {"JH3": ["lab variant"], "scRNA-seq": ["single-cell RNA sequencing"]}}
    merged = apply_lab_profile(user, lab)
    assert merged["lab_topics"] == ["genomics", "single-cell"]
    # 个人别名优先于实验室别名；实验室独有的别名保留
    assert merged["aliases"] == {"JH3": ["juvenile hormone III"],
                                 "scRNA-seq": ["single-cell RNA sequencing"]}
    assert "lab_topics" not in user  # 原 dict 不被修改


def test_apply_lab_profile_with_empty_lab():
    user = {"name": "A", "email": "a@x.com"}
    merged = apply_lab_profile(user, {})
    assert merged["lab_topics"] == []
    assert merged["aliases"] == {}
