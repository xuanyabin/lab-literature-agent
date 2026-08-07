"""Weekly Intelligence Summary：生成 150–250 字中文趋势总结。

两种输入模式（按用户全或无）：
- 无标注模式：只喂窗口内 Must Read / Important 论文的一句话新闻摘要
  （不喂原始摘要），控制 token 消耗；Reference 级论文不进入趋势总结；
- 星级模式：窗口内有标注时，只喂最新标注 ≥3 星的论文（带标注星级，
  按星级降序），prompt 要求侧重 4–5 星方向、3 星次要参考，体现用户偏好。

无符合输入时返回 ""（模板有占位文案）。
"""

from .llm import load_prompt
from .weekly_stats import parse_star

_TREND_CATEGORIES = ("Must Read", "Important")


def generate_weekly_summary(rows: list, llm, ratings: dict | None = None) -> str:
    """rows: db.get_week_recommendations 返回的记录（含 category/title/journal/news 字段）；
    ratings: 星级模式下的 {paper_id: 最新标注值}（None 表示无标注模式）。"""
    if ratings is None:
        focus = [r for r in rows if r["category"] in _TREND_CATEGORIES]
        if not focus:
            return ""
        prompt = load_prompt("weekly_report").safe_substitute(
            count=len(focus),
            papers_block=_papers_block(focus),
        )
    else:
        focus = [(r, s) for r in rows
                 if (s := parse_star(ratings.get(r["paper_id"]))) is not None and s >= 3]
        if not focus:
            return ""
        focus.sort(key=lambda t: (-t[1], -(t[0]["score"] or 0)))
        prompt = load_prompt("weekly_report_rated").safe_substitute(
            count=len(focus),
            papers_block=_rated_papers_block(focus),
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


def _rated_papers_block(focus: list) -> str:
    lines = []
    for i, (row, star) in enumerate(focus, 1):
        lines.append(
            f"--- 论文 {i} ---\n"
            f"标注星级：{'★' * star}（{star} 星）\n"
            f"标题：{row['title']}\n"
            f"期刊：{row['journal'] or '（未知）'}\n"
            f"一句话新闻摘要：{row['news'] or '（无）'}"
        )
    return "\n\n".join(lines)
