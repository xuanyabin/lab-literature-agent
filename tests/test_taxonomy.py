"""两层展示分类法（processing/taxonomy.py）测试：加载、校验、显示名与失败回退。"""

import logging

import pytest

from processing import taxonomy


@pytest.fixture()
def tax():
    return taxonomy.load_taxonomy()  # 真实 config/taxonomy.yaml


def test_load_real_config_has_4_categories_14_subcategories(tax):
    cats = tax["categories"]
    assert len(cats) == 4
    assert sum(len(c["subcategories"]) for c in cats.values()) == 15
    assert all(c["label_zh"] for c in cats.values())


def test_ordered_categories_fixed_order(tax):
    keys = [k for k, _ in taxonomy.ordered_categories(tax)]
    assert keys == ["genome_evolution_diversity", "cellular_spatial_biology",
                    "animal_adaptation_physiology", "computational_biology"]
    labels = [label for _, label in taxonomy.ordered_categories(tax)]
    assert labels == ["基因组演化与多样性", "细胞与空间生物学", "动物适应与生理", "计算生物学"]


def test_validate_valid_pair(tax):
    assert taxonomy.validate("cellular_spatial_biology", "brain_atlas", tax) == \
        ("cellular_spatial_biology", "brain_atlas")


def test_validate_empty_pair_is_legal(tax):
    assert taxonomy.validate("", "", tax) == ("", "")
    assert taxonomy.validate(None, None, tax) == ("", "")


def test_validate_invalid_pairs_fall_back(tax, caplog):
    bad = [
        ("genomics", "pangenomics"),                       # 大类 key 不存在
        ("genome_evolution_diversity", "spatial_omics"),   # 子类不属于该大类
        ("genome_evolution_diversity", ""),                # 只给大类
        ("", "pangenomics"),                               # 只给子类
    ]
    with caplog.at_level(logging.WARNING):
        for cat, sub in bad:
            assert taxonomy.validate(cat, sub, tax) == ("", "")
    assert caplog.records  # 每次非法回退都有 warning


def test_validate_with_empty_taxonomy_rejects_everything():
    assert taxonomy.validate("genome_evolution_diversity", "pangenomics", {}) == ("", "")


def test_subcategory_label(tax):
    assert taxonomy.subcategory_label("computational_biology", "ai_biology", tax) == "AI 生物学"
    assert taxonomy.subcategory_label("computational_biology", "pangenomics", tax) == ""
    assert taxonomy.subcategory_label("nope", "ai_biology", tax) == ""
    assert taxonomy.subcategory_label("", "", tax) == ""


def test_load_missing_file_returns_empty(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        tax = taxonomy.load_taxonomy(tmp_path / "missing.yaml")
    assert tax == {}
    assert taxonomy.ordered_categories(tax) == []
    assert any("未分类" in r.message for r in caplog.records)


def test_load_broken_structure_returns_empty(tmp_path, caplog):
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: bar\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert taxonomy.load_taxonomy(bad) == {}
