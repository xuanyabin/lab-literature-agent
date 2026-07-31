"""feedback.store 文件队列测试（tmp_path 隔离，不碰真实 feedback_data/）。"""

import hashlib
from datetime import datetime, timezone

import yaml

from feedback import store


def _entry(**kw):
    base = {"user_email": "a@x.com", "paper_id": 12, "value": "5"}
    base.update(kw)
    return base


def test_save_pending_writes_yaml_with_plain_email(tmp_path):
    path = store.save_pending(_entry(reason="不错"), tmp_path)
    assert path is not None
    assert path.parent == tmp_path / "pending"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["user_email"] == "a@x.com"  # 明文邮箱（学习按用户分组需要）
    assert data["paper_id"] == 12
    assert data["value"] == "5"
    assert data["reason"] == "不错"
    assert data["source"] == "email"  # 缺省来源
    assert data["timestamp"]


def test_save_pending_filename_format(tmp_path):
    path = store.save_pending(_entry(), tmp_path)
    digest = hashlib.sha256("a@x.com".encode("utf-8")).hexdigest()[:8]
    assert path.name == f"u{digest}_p12_v5.yaml"
    assert "a@x.com" not in path.name  # 文件名不含明文邮箱


def test_save_pending_idempotent_skip(tmp_path):
    assert store.save_pending(_entry(), tmp_path) is not None
    assert store.save_pending(_entry(), tmp_path) is None  # 同名已存在，幂等跳过
    assert len(list((tmp_path / "pending").glob("*.yaml"))) == 1


def test_save_pending_skipped_when_already_archived(tmp_path):
    """已归档（processed/）的同条反馈不再入队（对应 db UNIQUE 语义）。"""
    path = store.save_pending(_entry(), tmp_path)
    store.mark_processed(path, tmp_path)
    assert store.save_pending(_entry(), tmp_path) is None
    assert not list((tmp_path / "pending").glob("*.yaml"))


def test_save_pending_different_value_is_new_record(tmp_path):
    assert store.save_pending(_entry(value="4"), tmp_path) is not None
    assert store.save_pending(_entry(value="5"), tmp_path) is not None  # 改判另算一条
    assert len(list((tmp_path / "pending").glob("*.yaml"))) == 2


def test_load_pending_returns_entries_with_path(tmp_path):
    store.save_pending(_entry(), tmp_path)
    store.save_pending(_entry(paper_id=13, value="2"), tmp_path)
    entries = store.load_pending(tmp_path)
    assert len(entries) == 2
    assert {e["paper_id"] for e in entries} == {12, 13}
    assert all(e["path"].parent == tmp_path / "pending" for e in entries)


def test_load_pending_empty_and_missing_dir(tmp_path):
    assert store.load_pending(tmp_path) == []


def test_load_pending_skips_corrupt_file(tmp_path):
    store.save_pending(_entry(), tmp_path)
    (tmp_path / "pending" / "broken.yaml").write_text("foo: [1, 2", encoding="utf-8")
    entries = store.load_pending(tmp_path)
    assert len(entries) == 1  # 损坏文件跳过，正常文件不受影响


def test_mark_processed_archives_by_timestamp_month(tmp_path):
    path = store.save_pending(_entry(), tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["timestamp"] = "2026-03-15T08:00:00+00:00"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    dest = store.mark_processed(path, tmp_path)
    assert dest == tmp_path / "processed" / "2026-03" / path.name
    assert dest.exists() and not path.exists()


def test_mark_processed_falls_back_to_current_month(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir(parents=True)
    path = pending / "u12345678_p1_v1.yaml"
    path.write_text("user_email: a@x.com\npaper_id: 1\nvalue: '1'\n", encoding="utf-8")
    dest = store.mark_processed(path, tmp_path)  # 文件无 timestamp → 当前月
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert dest == tmp_path / "processed" / month / path.name
    assert dest.exists()
