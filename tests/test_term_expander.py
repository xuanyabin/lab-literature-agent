import json
import os
import time

import yaml

from processing.term_expander import (
    MAX_ALIASES_PER_TERM,
    apply_auto_terms,
    load_auto_terms,
    refresh_auto_terms,
)

USER = {
    "name": "Test", "email": "t@x.com",
    "keywords": ["honeybee"],
    "research_interest": ["gut microbiota"],
    "methods": [], "species": [],
}


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return json.dumps(self.payload)


def _write_user_yaml(tmp_path):
    path = tmp_path / "user001.yaml"
    path.write_text(yaml.dump(USER), encoding="utf-8")
    return path


def _refresh(tmp_path, llm, user_path=None):
    cache_dir = tmp_path / "auto_terms"
    return refresh_auto_terms("user001", USER, user_path, llm, cache_dir=cache_dir)


def test_refresh_creates_cache_when_missing(tmp_path):
    payload = {"honeybee": ["apis mellifera", "apis", "bee", "a. mellifera", "honey bee", "extra"]}
    auto = _refresh(tmp_path, FakeLLM(payload), _write_user_yaml(tmp_path))
    assert len(auto["expansion"]["honeybee"]) == MAX_ALIASES_PER_TERM  # 截断到 5 个
    assert auto["feedback_added"] == []
    assert (tmp_path / "auto_terms" / "user001.yaml").exists()


def test_refresh_skips_fresh_cache(tmp_path):
    user_path = _write_user_yaml(tmp_path)
    first = FakeLLM({"honeybee": ["apis"]})
    _refresh(tmp_path, first, user_path)
    second = FakeLLM({"honeybee": ["changed"]})
    auto = _refresh(tmp_path, second, user_path)
    assert second.calls == 0  # 缓存新鲜且用户 yaml 未改，不调 LLM
    assert auto["expansion"]["honeybee"] == ["apis"]


def test_refresh_when_user_yaml_newer(tmp_path):
    user_path = _write_user_yaml(tmp_path)
    _refresh(tmp_path, FakeLLM({"honeybee": ["apis"]}), user_path)
    future = time.time() + 10
    os.utime(user_path, (future, future))
    second = FakeLLM({"honeybee": ["changed"]})
    auto = _refresh(tmp_path, second, user_path)
    assert second.calls == 1
    assert auto["expansion"]["honeybee"] == ["changed"]


def test_refresh_when_cache_older_than_ttl(tmp_path):
    _refresh(tmp_path, FakeLLM({"honeybee": ["apis"]}), None)
    cache = tmp_path / "auto_terms" / "user001.yaml"
    old = time.time() - 8 * 86400
    os.utime(cache, (old, old))
    second = FakeLLM({"honeybee": ["changed"]})
    auto = _refresh(tmp_path, second, None)
    assert second.calls == 1
    assert auto["expansion"]["honeybee"] == ["changed"]


def test_refresh_llm_failure_keeps_old_cache(tmp_path):
    _refresh(tmp_path, FakeLLM({"honeybee": ["apis"]}), None)
    cache = tmp_path / "auto_terms" / "user001.yaml"
    old = time.time() - 8 * 86400
    os.utime(cache, (old, old))

    class BadLLM:
        def complete(self, prompt):
            raise RuntimeError("LLM down")

    auto = _refresh(tmp_path, BadLLM(), None)
    assert auto["expansion"]["honeybee"] == ["apis"]  # 旧缓存原样保留


def test_refresh_llm_failure_without_cache_returns_empty(tmp_path):
    class BadLLM:
        def complete(self, prompt):
            raise RuntimeError("LLM down")

    auto = _refresh(tmp_path, BadLLM(), None)
    assert auto == {"expansion": {}, "feedback_added": []}
    assert not (tmp_path / "auto_terms" / "user001.yaml").exists()


def test_refresh_preserves_feedback_added(tmp_path):
    cache_dir = tmp_path / "auto_terms"
    cache_dir.mkdir()
    (cache_dir / "user001.yaml").write_text(
        yaml.dump({"updated": "2026-07-01", "expansion": {"honeybee": ["apis"]},
                   "feedback_added": ["microbiome"]}),
        encoding="utf-8",
    )
    old = time.time() - 8 * 86400
    os.utime(cache_dir / "user001.yaml", (old, old))
    auto = _refresh(tmp_path, FakeLLM({"honeybee": ["changed"]}), None)
    assert auto["expansion"]["honeybee"] == ["changed"]
    assert auto["feedback_added"] == ["microbiome"]  # 刷新不清空反馈新增词


def test_load_auto_terms_missing(tmp_path):
    assert load_auto_terms("nobody", cache_dir=tmp_path) == {"expansion": {}, "feedback_added": []}


def test_apply_auto_terms_merges_aliases_with_personal_priority():
    user = {"keywords": ["honeybee"], "aliases": {"honeybee": ["Apis"]}}
    auto = {"expansion": {"honeybee": ["apis", "bee"], "gut microbiota": ["microbiome"]},
            "feedback_added": []}
    merged = apply_auto_terms(user, auto)
    # 个人别名在前；扩展词追加且大小写不敏感去重（"apis" 与个人 "Apis" 重复）
    assert merged["aliases"]["honeybee"] == ["Apis", "bee"]
    assert merged["aliases"]["gut microbiota"] == ["microbiome"]  # 新原词并入
    assert user["aliases"] == {"honeybee": ["Apis"]}  # 原配置不被修改


def test_apply_auto_terms_appends_feedback_added_dedup():
    user = {"keywords": ["Honeybee"]}
    auto = {"expansion": {}, "feedback_added": ["honeybee", "microbiome"]}
    merged = apply_auto_terms(user, auto)
    assert merged["keywords"] == ["Honeybee", "microbiome"]
