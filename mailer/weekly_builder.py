"""组装每周情报报告 HTML 邮件。"""

from mailer.digest_builder import _group_items, _show_module_heads
from mailer.template_renderer import escape, render

_CATEGORY_CLASS = {
    "Must Read": "cat-must",
    "Important": "cat-important",
    "Reference": "cat-reference",
    "Ignore": "cat-ignore",
}


def _badges(row):
    """模块名 + 文献类型两个 badge：module_label / paper_type 缺失时不渲染（旧数据回退）。"""
    out = ""
    label = row.get("module_label") or ""
    if label:
        out += f'<span class="badge cat-module">{escape(label)}</span>'
    paper_type = row.get("paper_type") or ""
    if paper_type:
        out += f'<span class="badge cat-type">{escape(paper_type)}</span>'
    return out


def _paper_rows(rows):
    """本周 Must Read / Important 论文清单（行格式与日报 news-table 一致）。

    按 module_label 分组（组序=首现序、"其他"沉底、仅"其他"一组时不渲染小标题），
    序号跨组连续；每行标题前加模块名 + 文献类型 badge。"""
    items = [r for r in rows if r["category"] in ("Must Read", "Important")]
    if not items:
        return '<tr><td colspan="2">本周没有 Must Read / Important 论文</td></tr>'
    groups = _group_items(items)
    show_heads = _show_module_heads(groups)
    parts = []
    for label, group in groups:
        if show_heads:
            parts.append(f'      <tr><td colspan="2" class="module-head">{escape(label)}</td></tr>')
        for index, row in group:
            category = row["category"]
            badge_cls = _CATEGORY_CLASS.get(category, "cat-reference")
            news = escape(row["news"]) if row["news"] else "本周暂无新闻解读"
            parts.append(
                "      <tr>\n"
                f'        <td class="num">{index}</td>\n'
                "        <td>\n"
                f'          <div class="news-head"><span class="badge {badge_cls}">{escape(category)}</span>'
                f'{_badges(row)}\n'
                f'            <a class="title-link" href="{escape(row["url"])}">{escape(row["title"])}</a></div>\n'
                f'          <div class="news">{news}</div>\n'
                f'          <div class="meta">{escape(row["journal"] or "")} · '
                f'{escape(row["sent_date"] or row["date"] or "")}</div>\n'
                "        </td>\n"
                "      </tr>"
            )
    return "\n".join(parts)


def _stats_rows(stats):
    lines = []
    by_category = stats["by_category"]
    lines.append(
        '<tr><td class="stats-label">收录论文</td>'
        f'<td>共 {stats["total"]} 篇'
        f'（Must Read {by_category.get("Must Read", 0)} / '
        f'Important {by_category.get("Important", 0)} / '
        f'Reference {by_category.get("Reference", 0)}）</td></tr>'
    )
    by_tier = stats["by_tier"]
    lines.append(
        '<tr><td class="stats-label">期刊分层</td>'
        f'<td>顶刊 {by_tier["t0"]} 篇 / 领域权威 {by_tier["t1"]} 篇 / '
        f'其他 {by_tier["other"]} 篇</td></tr>'
    )
    if stats["top_journals"]:
        journals = "、".join(
            f"{escape(name)}（{count}）" for name, count in stats["top_journals"]
        )
        lines.append(
            f'<tr><td class="stats-label">主要来源期刊</td><td>{journals}</td></tr>'
        )
    if stats["top_keywords"]:
        keywords = "、".join(
            f"{escape(name)}（{count}）" for name, count in stats["top_keywords"]
        )
        lines.append(
            f'<tr><td class="stats-label">高频关键词</td><td>{keywords}</td></tr>'
        )
    return "".join(lines)


def _trends_rows(trends):
    """阅读趋势块：窗口内反馈正/中/负分布 + 当前有效学习词 Top（复用 stats-table 样式）。"""
    fb = trends["feedback"]
    lines = [
        '<tr><td class="stats-label">反馈分布</td>'
        f'<td>共 {fb["total"]} 条'
        f'（正向 {fb["positive"]} / 中性 {fb["neutral"]} / 负向 {fb["negative"]}）</td></tr>'
    ]
    if trends["top_terms"]:
        terms = "、".join(
            f"{escape(term)}（{weight:.2f}）" for term, weight in trends["top_terms"]
        )
        lines.append(f'<tr><td class="stats-label">学习热词</td><td>{terms}</td></tr>')
    return "".join(lines)


def build_weekly_html(user_name, week_start, week_end, rows, trend_summary, stats, trends):
    """rows 为 get_week_recommendations 结果；trend_summary 为 LLM 周度总结（可为空）；
    trends 为 compute_reading_trends 的阅读趋势统计。"""
    context = {
        "week_start": escape(week_start),
        "week_end": escape(week_end),
        "user_name": escape(user_name),
        "total": str(stats["total"]),
        "trend_summary": escape(trend_summary)
        or "本周高价值论文较少，未生成趋势总结。",
        "stats_rows": _stats_rows(stats),
        "trends_rows": _trends_rows(trends),
        "paper_rows": _paper_rows(rows),
    }
    return render("weekly_digest.html", context)
