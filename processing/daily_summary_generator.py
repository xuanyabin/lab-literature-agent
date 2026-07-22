"""Daily Intelligence Summary：基于当日全部推荐论文生成 100–200 字中文价值总结。"""

from .llm import load_prompt


def generate_daily_summary(items: list[dict], llm) -> str:
    """items: [{"paper": Paper, "news": str, "category": str, ...}, ...]（按推荐排序）。"""
    if not items:
        return ""
    prompt = load_prompt("daily_value_summary").safe_substitute(
        count=len(items),
        papers_block=_papers_block(items),
    )
    return llm.complete(prompt).strip().strip('"').strip()


def _papers_block(items: list[dict]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        p = it["paper"]
        keywords = ", ".join(p.keywords) or "（无）"
        lines.append(
            f"--- 论文 {i} ---\n"
            f"推荐等级：{it.get('category', '')}\n"
            f"标题：{p.title}\n"
            f"期刊：{p.journal or '（未知）'}\n"
            f"关键词：{keywords}\n"
            f"一句话新闻摘要：{it.get('news', '')}\n"
            f"原始摘要：{p.abstract or '（无摘要）'}"
        )
    return "\n\n".join(lines)
