"""组装每周情报报告 HTML 邮件。"""

from mailer.digest_builder import _group_items, _show_module_heads
from mailer.template_renderer import escape, render
from processing.weekly_stats import parse_star

_CATEGORY_CLASS = {
    "Must Read": "cat-must",
    "Important": "cat-important",
    "Reference": "cat-reference",
    "Ignore": "cat-ignore",
}


def _badges(row):
    """子类 + 文献类型两个 badge：subcategory_label / paper_type 缺失时不渲染（旧数据回退）。"""
    out = ""
    label = row.get("subcategory_label") or ""
    if label:
        out += f'<span class="badge cat-module">{escape(label)}</span>'
    paper_type = row.get("paper_type") or ""
    if paper_type:
        out += f'<span class="badge cat-type">{escape(paper_type)}</span>'
    return out


def _paper_rows(rows, ratings=None):
    """本周重点论文清单（行格式与日报 news-table 一致）。

    两种模式（按用户全或无，由调用方判定后通过 ratings 传入）：
    - 无标注模式（ratings=None）：本周 Must Read / Important 清单，行为同原实现；
    - 星级模式（ratings 为 {paper_id: 最新标注值}）：只收录最新标注 ≥3 星的论文，
      模块内按标注星级降序、同星级按推荐 score 降序，行内追加 ★n 标注星级。

    两种模式都按 taxonomy 大类分组（组序=taxonomy.yaml 固定大类序、空大类不渲染、
    未分类归"其他"沉底、仅"其他"一组时不渲染小标题），序号跨组连续；
    每行标题前加子类 + 文献类型 badge。"""
    if ratings is None:
        items = [r for r in rows if r["category"] in ("Must Read", "Important")]
        empty_text = "本周没有 Must Read / Important 论文"
    else:
        items = []
        for r in rows:
            star = parse_star(ratings.get(r["paper_id"]))
            if star is not None and star >= 3:
                r["star"] = star
                items.append(r)
        items.sort(key=lambda r: (-r["star"], -(r["score"] or 0)))
        empty_text = "本周期内没有 3 星及以上的标注论文"
    if not items:
        return f'<tr><td colspan="2">{empty_text}</td></tr>'
    groups = _group_items(items)
    show_heads = _show_module_heads(groups)
    parts = []
    for label, group in groups:
        if show_heads:
            parts.append(f'      <tr><td colspan="2" class="module-head">{escape(label)}</td></tr>')
        for index, row in group:
            category = row["category"]
            badge_cls = _CATEGORY_CLASS.get(category, "cat-reference")
            star = row.get("star")
            star_badge = f'<span class="badge cat-star">★{star}</span>' if star else ""
            news = escape(row["news"]) if row["news"] else "本周暂无新闻解读"
            parts.append(
                "      <tr>\n"
                f'        <td class="num">{index}</td>\n'
                "        <td>\n"
                f'          <div class="news-head"><span class="badge {badge_cls}">{escape(category)}</span>'
                f'{_badges(row)}{star_badge}\n'
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


def build_weekly_html(user_name, week_start, week_end, rows, trend_summary, stats, trends,
                      ratings=None):
    """rows 为 get_week_recommendations 结果；trend_summary 为 LLM 周度总结（可为空）；
    trends 为 compute_reading_trends 的阅读趋势统计；ratings 为星级模式下的
    {paper_id: 最新标注值}（None 表示无标注模式，清单维持 Must Read / Important）。"""
    star_mode = ratings is not None
    context = {
        "week_start": escape(week_start),
        "week_end": escape(week_end),
        "user_name": escape(user_name),
        "total": str(stats["total"]),
        "trend_summary": escape(trend_summary)
        or "本周高价值论文较少，未生成趋势总结。",
        "trend_basis": ("基于你标注 ★3 及以上的论文生成 · 侧重 4–5 星偏好方向"
                        if star_mode else
                        "基于本周 Must Read / Important 论文生成 · 下周跟踪线索"),
        "list_basis": ("你标注 ★3 及以上的论文 · 模块内按星级降序"
                       if star_mode else
                       "Must Read / Important · 周末集中补读"),
        "stats_rows": _stats_rows(stats),
        "trends_rows": _trends_rows(trends),
        "paper_rows": _paper_rows(rows, ratings),
    }
    return render("weekly_digest.html", context)
