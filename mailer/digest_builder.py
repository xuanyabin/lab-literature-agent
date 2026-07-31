"""组装每日文献情报邮件的各段内容（总览 / 可展开论文卡片 / 价值总结 / 反馈入口），渲染交给 template_renderer。

每篇论文一张卡片：默认只显示一句话新闻概要，卡片内 <details> 折叠完整信息
（作者 / 摘要 / 推荐理由）；不支持 <details> 的邮件客户端会默认全部展开，可读性不受影响。

反馈入口（全部由 python -m feedback 经 IMAP 收集学习）：
- 逐篇五星：每张卡片底部内嵌 ⭐1-5 mailto 链接（主题预填
  "[FB] u=<用户邮箱> p=<论文id> v=<星级>"），接收人点击拉起邮件草稿，发送即完成反馈；
- 批量标注：整封邮件一个"一键反馈"mailto（Part 3），回信主题带 [FB] token 与日期，
  正文按编号标注 1-5 星、以 "+" 开头的行是新增关键词。
两条路汇入同一个反馈收件箱（feedback_email），都要求调用方提供 user_email 且 items 非空。
"""

from pathlib import Path
from urllib.parse import quote

import yaml

from mailer.template_renderer import escape, render

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_EMAIL_CONFIG = BASE_DIR / "config" / "email.yaml"

_DEFAULT_CONFIG = {
    "daily_paper_number": 15,
    "show_translation": True,
    "show_keywords": True,
    "show_doi": True,
    "feedback_email": "",
}

_CATEGORY_CLASS = {
    "Must Read": "cat-must",
    "Important": "cat-important",
    "Reference": "cat-reference",
    "Ignore": "cat-ignore",
}

# 中文四段结构化摘要：item 键 → 卡片小标签（为空的段不渲染）
_SECTION_LABELS = [
    ("background", "背景"),
    ("methods", "研究方法"),
    ("results", "研究结果"),
    ("significance", "研究意义"),
]


def load_email_config(path: Path = DEFAULT_EMAIL_CONFIG) -> dict:
    """读取 config/email.yaml，缺省字段回退到默认值。"""
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {**_DEFAULT_CONFIG, **cfg}


def build_digest_html(user_name: str, digest_date: str, items: list[dict],
                      daily_summary: str, config: dict | None = None,
                      user_email: str = "", overview: dict | None = None) -> str:
    """items: [{"paper": Paper, "analysis": dict, "news": str, "title_zh": str,
               "background": str, "methods": str, "results": str,
               "significance": str, "category": str}, ...]（按推荐排序）
    overview: {"days", "pool_total", "matched", "pushed", "must_read",
               "important", "reference"}，为 None 时不渲染开头总览块。"""
    cfg = {**_DEFAULT_CONFIG, **(config or {})}
    context = {
        "user_name": escape(user_name),
        "date": escape(digest_date),
        "count": str(len(items)),
        "overview_block": _overview_block(overview) if overview else "",
        "paper_cards": "\n".join(_paper_card(i, it, cfg, user_email) for i, it in enumerate(items, 1)),
        "daily_summary": escape(daily_summary) if daily_summary else "（今日价值总结生成失败，请查看上方论文列表。）",
        "feedback_block": _feedback_block(user_email, digest_date, len(items), cfg),
    }
    return render("daily_digest.html", context)


def _overview_block(overview: dict) -> str:
    """日报开头总览：全库新增 / 关键词命中 / 本次推送（含定级分布）。"""
    days = overview.get("days", 1)
    span = "昨日" if days == 1 else f"过去 {days} 天"
    text = (
        f"{span}全库新增 {overview.get('pool_total', 0)} 篇 · "
        f"命中您的关键词 {overview.get('matched', 0)} 篇 · "
        f"本次推送 {overview.get('pushed', 0)} 篇"
        f"（必读 {overview.get('must_read', 0)} · 重要 {overview.get('important', 0)} · "
        f"参考 {overview.get('reference', 0)}）"
    )
    return f'<div class="overview">{escape(text)}</div>'


def _badge(category: str) -> str:
    cls = _CATEGORY_CLASS.get(category, "cat-reference")
    return f'<span class="badge {cls}">{escape(category or "Reference")}</span>'


def _feedback_block(user_email: str, digest_date: str, count: int, cfg: dict) -> str:
    """Part 3 批量标注入口：一封 mailto 按编号标注星级（正文预填编号行与 +关键词 行）。"""
    if not user_email or count <= 0:
        return ""
    if not cfg["feedback_email"]:
        return ""
    subject = quote(f"[FB] u={user_email} d={digest_date}")
    lines = [
        "请直接在编号后填写 1-5 星评分（⭐1 完全不相关 – ⭐5 非常重要；只填想评的编号，其余留空）：",
        "",
    ]
    lines += [f"{i:02d}: " for i in range(1, count + 1)]
    lines += [
        "",
        "如需新增关键词，请在下方 + 号后填写（每行一个，可用逗号分隔）：",
        "+",
    ]
    body = quote("\n".join(lines))
    href = f"mailto:{cfg['feedback_email']}?subject={subject}&body={body}"
    return f"""    <h2>Part 3 · 一键反馈</h2>
    <p class="section-sub">只回一封邮件 · 按编号标注星级，帮助我们越推越准</p>
    <div class="feedback-box">
      点击下面的按钮打开反馈邮件草稿，在编号后填 1-5 星（只评想评的即可），发送即完成：<br>
      <a class="feedback-btn" href="{href}">一键标注今日 {count} 篇论文</a>
    </div>"""


_RATING_LABELS = {1: "完全不相关", 2: "不太相关", 3: "一般", 4: "比较重要", 5: "非常重要"}


def _star_row(user_email: str, paper_id, cfg: dict) -> str:
    """卡片底部 ⭐1-5 反馈链接（mailto：点击拉起预填主题的邮件草稿，发送即完成反馈）；
    未配置 feedback_email、缺 user_email 或 paper_id（dry-run）时不渲染。"""
    if not (cfg["feedback_email"] and user_email and paper_id):
        return ""
    links = []
    for n in (1, 2, 3, 4, 5):
        subject = quote(f"[FB] u={user_email} p={paper_id} v={n}")
        href = f"mailto:{cfg['feedback_email']}?subject={subject}"
        links.append(f'<a class="star" href="{href}" title="{_RATING_LABELS[n]}">⭐{n}</a>')
    return ('<div class="stars"><span class="abs-label">重要性反馈</span>'
            + " ".join(links)
            + '<span class="stars-hint">点击后发送邮件即完成反馈 · ⭐1 完全不相关 – ⭐5 非常重要</span></div>')


def _paper_card(i: int, it: dict, cfg: dict, user_email: str = "") -> str:
    """单篇论文卡片：默认可见区为标题 + 一句话新闻概要 + 期刊日期，
    <details> 折叠区内放作者 / DOI / 关键词 / 推荐理由 / 中文四段摘要。
    五星反馈在折叠区外，不展开也能直接点击。"""
    p = it["paper"]
    head = f'Rank {i} {_badge(it.get("category", "Reference"))}'
    if it.get("score") is not None:
        head += f' · {it["score"]} 分'
    rows = [
        f'<div class="card-head">{head}</div>',
        f'<div class="card-title"><a class="title-link" href="{escape(p.url)}">{escape(p.title)}</a></div>',
    ]
    if cfg["show_translation"] and it.get("title_zh"):
        rows.append(f'<div class="card-title-zh">{escape(it["title_zh"])}</div>')
    if it.get("news"):
        rows.append(f'<div class="news">{escape(it["news"])}</div>')
    rows.append(f'<div class="meta">{escape(p.journal)} · {escape(p.date)}</div>')

    detail = [f'<div class="meta">{escape(p.authors)}</div>']
    if cfg["show_doi"]:
        detail.append(f'<div class="meta">DOI: {escape(p.doi or "—")}</div>')
    detail.append(f'<div class="meta"><a href="{escape(p.url)}">{escape(p.url)}</a></div>')
    if cfg["show_keywords"]:
        keywords = "、".join(p.keywords) or "—"
        detail.append(f'<div class="meta">Keywords: {escape(keywords)}</div>')
    if it.get("reason"):
        detail.append(f'<div class="reason"><span class="abs-label">推荐理由</span>{escape(it["reason"])}</div>')
    if cfg["show_translation"]:
        # 只展示中文四段结构化摘要（为空的段不渲染），不再显示英文摘要
        for key, label in _SECTION_LABELS:
            if it.get(key):
                detail.append(f'<div class="abstract"><span class="abs-label">{label}</span>{escape(it[key])}</div>')
    else:
        detail.append(f'<div class="abstract"><span class="abs-label">Abstract</span>{escape(p.abstract or "（无摘要）")}</div>')
    rows.append('<details class="card-details"><summary class="detail-toggle">展开详情 · 作者 / 摘要 / 推荐理由</summary>\n'
                + "\n".join(detail) + '\n</details>')

    star = _star_row(user_email, it.get("paper_id"), cfg)
    if star:
        rows.append(star)
    body = "\n      ".join(rows)
    return f'    <div class="card">\n      {body}\n    </div>'
