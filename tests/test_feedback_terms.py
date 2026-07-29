"""processing.term_expander 的反馈新增关键词（B4）测试：回信 "+关键词" 行追加到自动词表。"""

import pytest
import yaml

from processing.term_expander import (
    add_feedback_terms, clean_feedback_term, load_auto_terms, slug_for_email,
)


@pytest.fixture
def dirs(tmp_path):
    """两个用户 yaml（user002 的 email 刻意混大小写）+ 独立自动词表目录。"""
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    (users_dir / "user001.yaml").write_text(
        yaml.dump({"name": "Tester", "email": "a@x.com"}, allow_unicode=True),
        encoding="utf-8")
    (users_dir / "user002.yaml").write_text(
        yaml.dump({"name": "Other", "email": "B@x.com"}, allow_unicode=True),
        encoding="utf-8")
    return users_dir, tmp_path / "auto_terms"


def test_slug_for_email_case_insensitive(dirs):
    users_dir, _ = dirs
    assert slug_for_email("a@x.com", users_dir) == "user001"
    assert slug_for_email("b@x.COM", users_dir) == "user002"
    assert slug_for_email("nobody@x.com", users_dir) is None
    assert slug_for_email("", users_dir) is None


def test_clean_feedback_term_whitelist():
    assert clean_feedback_term("  CRISPR  ") == "CRISPR"
    assert clean_feedback_term("单细胞测序") == "单细胞测序"
    # 连字符、下划线、字母数字、空格保留
    assert clean_feedback_term("single-cell RNA_seq") == "single-cell RNA_seq"
    # 正则危险字符与标点剔除
    assert clean_feedback_term("CRISPR.*(cas9)?") == "CRISPRcas9"
    assert clean_feedback_term("a/b\\c") == "abc"
    # 超长词与清洗后为空的词丢弃
    assert clean_feedback_term("x" * 61) == ""
    assert clean_feedback_term("+*?") == ""


def test_add_terms_creates_file_and_appends(dirs):
    users_dir, cache_dir = dirs
    added = add_feedback_terms("a@x.com", ["CRISPR", "单细胞测序"], users_dir, cache_dir)
    assert added == ["CRISPR", "单细胞测序"]
    auto = load_auto_terms("user001", cache_dir)
    assert auto["feedback_added"] == ["CRISPR", "单细胞测序"]
    assert auto["expansion"] == {}  # 新建文件带空 expansion


def test_add_terms_dedupes_case_insensitive(dirs):
    users_dir, cache_dir = dirs
    add_feedback_terms("a@x.com", ["crispr"], users_dir, cache_dir)
    added = add_feedback_terms("a@x.com", ["CRISPR", " crispr ", "ATAC-seq"],
                               users_dir, cache_dir)
    assert added == ["ATAC-seq"]
    assert load_auto_terms("user001", cache_dir)["feedback_added"] == ["crispr", "ATAC-seq"]


def test_add_terms_drops_unsafe_and_overlong(dirs):
    users_dir, cache_dir = dirs
    added = add_feedback_terms("a@x.com", ["valid term", "x" * 61, "+*?"],
                               users_dir, cache_dir)
    assert added == ["valid term"]
    assert load_auto_terms("user001", cache_dir)["feedback_added"] == ["valid term"]


def test_add_terms_preserves_existing_expansion(dirs):
    users_dir, cache_dir = dirs
    cache_dir.mkdir()
    (cache_dir / "user001.yaml").write_text(
        "# 自动维护，请勿手改\n" + yaml.dump(
            {"updated": "2026-07-01", "expansion": {"honeybee": ["Apis mellifera"]},
             "feedback_added": ["microbiome"]}, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    add_feedback_terms("a@x.com", ["CRISPR"], users_dir, cache_dir)
    auto = load_auto_terms("user001", cache_dir)
    assert auto["expansion"] == {"honeybee": ["Apis mellifera"]}
    assert auto["feedback_added"] == ["microbiome", "CRISPR"]


def test_add_terms_unknown_sender_skipped(dirs, caplog):
    users_dir, cache_dir = dirs
    with caplog.at_level("WARNING"):
        assert add_feedback_terms("stranger@x.com", ["CRISPR"], users_dir, cache_dir) == []
    assert "不匹配任何用户" in caplog.text
    # 未知发件人不落盘任何文件
    assert not cache_dir.exists() or list(cache_dir.iterdir()) == []
