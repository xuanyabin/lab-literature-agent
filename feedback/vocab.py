"""学习词表的读取侧：时间衰减 + 有效词过滤（Phase 5）。

learned_terms 表存的是未衰减的原始权重；有效权重在读取时按
半衰期衰减计算（不回写数据库），低于 min_effective 的词视为失效，
不参与检索与打分。配置见 config/scoring.yaml 的 learned 节。
"""

import sqlite3
from datetime import date
from pathlib import Path

import yaml

from database.db import load_learned_terms

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SCORING_CONFIG = BASE_DIR / "config" / "scoring.yaml"

DEFAULT_LEARNED_CONFIG = {
    "promote_support": 2,
    "initial_weight": 1.0,
    "boost": 0.5,
    "max_weight": 3.0,
    "negative_factor_weak": 0.5,
    "negative_factor_strong": 0.25,
    "half_life_days": 30,
    "min_effective": 0.3,
    "score_cap": 6,
}


def load_learned_config(path: Path = DEFAULT_SCORING_CONFIG) -> dict:
    """读取 scoring.yaml 的 learned 节，缺省字段回退默认值。"""
    p = Path(path)
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {**DEFAULT_LEARNED_CONFIG, **((cfg or {}).get("learned") or {})}


def effective_weight(weight: float, last_seen: str, today: date | None = None,
                     half_life_days: float = 30) -> float:
    """有效权重 = 原始权重 × 0.5^(距今天数/半衰期)；last_seen 取日期部分。"""
    today = today or date.today()
    try:
        seen = date.fromisoformat((last_seen or "")[:10])
    except ValueError:
        return 0.0
    days = max((today - seen).days, 0)
    return weight * 0.5 ** (days / half_life_days)


def load_active_terms(conn: sqlite3.Connection, user_email: str,
                      config: dict | None = None, today: date | None = None) -> list[tuple[str, float]]:
    """该用户当前有效的学习词：[(词, 有效权重)]，按权重降序，失效词已过滤。"""
    cfg = {**DEFAULT_LEARNED_CONFIG, **(config or {})}
    terms = []
    for row in load_learned_terms(conn, user_email):
        eff = effective_weight(row["weight"], row["last_seen"], today, cfg["half_life_days"])
        if eff >= cfg["min_effective"]:
            terms.append((row["term"], eff))
    terms.sort(key=lambda x: -x[1])
    return terms
