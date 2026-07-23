"""周报统计（纯数据，不耗 LLM）：定级分布 / 期刊分层 / Top 期刊 / 高频关键词。"""

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
