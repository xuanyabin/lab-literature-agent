from datetime import date

import pytest

from database.db import connect, upsert_learned_term
from feedback.vocab import (
    DEFAULT_LEARNED_CONFIG, effective_weight, load_active_terms,
    load_learned_config,
)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def test_effective_weight_decays_by_half_life():
    today = date(2026, 7, 23)
    assert effective_weight(2.0, "2026-07-23", today, half_life_days=30) == 2.0
    assert effective_weight(2.0, "2026-06-23", today, half_life_days=30) == 1.0
    assert effective_weight(2.0, "2026-05-24", today, half_life_days=30) == 0.5


def test_effective_weight_handles_bad_date_and_future():
    today = date(2026, 7, 23)
    assert effective_weight(2.0, "", today) == 0.0
    assert effective_weight(2.0, "not-a-date", today) == 0.0
    # last_seen 在未来时不放大（天数下限为 0）
    assert effective_weight(2.0, "2026-08-01", today) == 2.0


def test_load_active_terms_filters_and_sorts(conn):
    cfg = dict(DEFAULT_LEARNED_CONFIG)
    today = date(2026, 7, 23)
    upsert_learned_term(conn, "a@x.com", "strong", 2.0, 5, "2026-07-23")
    upsert_learned_term(conn, "a@x.com", "weak", 0.4, 2, "2026-06-23")  # 衰减后 0.2 < 0.3
    upsert_learned_term(conn, "a@x.com", "candidate", 0.0, 1, "2026-07-23")  # 未提权
    upsert_learned_term(conn, "b@x.com", "other-user", 3.0, 9, "2026-07-23")

    terms = load_active_terms(conn, "a@x.com", cfg, today)
    assert terms == [("strong", 2.0)]  # weak / candidate 失效，其他用户隔离


def test_load_learned_config_defaults(tmp_path):
    cfg = load_learned_config(tmp_path / "missing.yaml")
    assert cfg == DEFAULT_LEARNED_CONFIG
