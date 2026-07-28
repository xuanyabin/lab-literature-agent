"""周报/月报统计（纯数据，不耗 LLM）：定级分布 / 期刊分层 / Top 期刊 / 高频关键词 / 阅读趋势。"""

import json
from collections import Counter

from recommendation.scorer import _normalize_journal


def compute_stats(rows: list, journal_tiers: dict[str, str]) -> dict:
    """rows: db.get_week_recommendations 返回的记录（含 category/journal/keywords 字段）。

    返回 {"total", "by_category", "by_tier", "top_journals", "top_keywords"}：
    by_category/by_tier 为 {名称: 篇数}；top_journals/top_keywords 为 [(名称, 篇数)]。
    """
    by_category: Counter = Counter()
    by_tier: Counter = Counter()
    journals: Counter = Counter()
    keywords: Counter = Counter()
    for row in rows:
        by_category[row["category"] or "Reference"] += 1
        journal = (row["journal"] or "").strip() or "（未知）"
        journals[journal] += 1
        tier = (journal_tiers or {}).get(_normalize_journal(journal))
        by_tier[tier or "other"] += 1
        try:
            keywords.update(k for k in json.loads(row["keywords"] or "[]") if k)
        except (TypeError, ValueError):
            continue
    return {
        "total": len(rows),
        "by_category": dict(by_category),
        "by_tier": {"t0": by_tier.get("t0", 0), "t1": by_tier.get("t1", 0),
                    "other": by_tier.get("other", 0)},
        "top_journals": journals.most_common(5),
        "top_keywords": keywords.most_common(10),
    }


# 反馈标注 → 正/中/负三桶：兼容旧四值（relevant/save 正、already_read 中、
# not_relevant 负）与五星字符串值（"4"/"5" 正、"3" 中、"1"/"2" 负）
_FEEDBACK_BUCKET = {
    "relevant": "positive",
    "save": "positive",
    "already_read": "neutral",
    "not_relevant": "negative",
    "4": "positive",
    "5": "positive",
    "3": "neutral",
    "1": "negative",
    "2": "negative",
}


def normalize_feedback_value(value: str) -> str | None:
    """把反馈标注归一化为 positive / neutral / negative；无法识别的值返回 None（统计时跳过）。"""
    return _FEEDBACK_BUCKET.get(str(value or "").strip().lower())


def compute_reading_trends(feedback_rows: list, active_terms: list,
                           top_n: int = 5) -> dict:
    """阅读趋势统计（窗口内、按用户）：反馈正/中/负分桶 + 当前有效学习词 Top。

    feedback_rows: db.get_feedback_since 返回的记录（含 value 字段）；
    active_terms: feedback.vocab.load_active_terms 的结果 [(词, 有效权重)]（已按用户过滤、
    已按有效权重降序）；无法识别的反馈值跳过且不计入总数。
    返回 {"feedback": {"positive", "neutral", "negative", "total"}, "top_terms": [(词, 有效权重)]}。
    """
    feedback = {"positive": 0, "neutral": 0, "negative": 0}
    for row in feedback_rows:
        bucket = normalize_feedback_value(row["value"])
        if bucket is not None:
            feedback[bucket] += 1
    feedback["total"] = sum(feedback.values())
    return {"feedback": feedback, "top_terms": list(active_terms[:top_n])}
