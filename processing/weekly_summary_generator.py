"""Weekly Intelligence Summary：基于本周 Must Read / Important 论文生成 150–250 字中文趋势总结。

只喂定级高的一句话新闻摘要（不喂原始摘要），控制 token 消耗；
Reference 级论文不进入趋势总结。
"""

from .llm import load_prompt

_TREND_CATEGORIES = ("Must Read", "Important")


def generate_weekly_summary(rows: list, llm) -> str:
    """rows: db.get_week_recommendations 返回的记录（含 category/title/journal/news 字段）。"""
    focus = [r for r in rows if r["category"] in _TREND_CATEGORIES]
    if not focus:
        return ""
    prompt = load_prompt("weekly_report").safe_substitute(
        count=len(focus),
        papers_block=_papers_block(focus),
    )
    return llm.complete(prompt).strip().strip('"').strip()


def _papers_block(rows: list) -> str:
    lines = []
    for i, row in enumerate(rows, 1):
        lines.append(
            f"--- 论文 {i} ---\n"
            f"推荐等级：{row['category']}\n"
            f"标题：{row['title']}\n"
            f"期刊：{row['journal'] or '（未知）'}\n"
            f"一句话新闻摘要：{row['news'] or '（无）'}"
        )
    return "\n\n".join(lines)
