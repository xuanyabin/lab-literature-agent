"""组装每日文献情报邮件的三段内容（新闻摘要 / 详细卡片 / 价值总结），渲染交给 template_renderer。

Phase 5 起论文卡片底部带反馈链接（相关/不相关/已读/收藏）：mailto 回信，
主题带 [FB] token，由 python -m feedback 收集学习。仅当调用方提供
user_email、config 含 feedback_email 且 item 带 paper_id（已入库）时渲染。
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

_FEEDBACK_CHOICES = [
    ("相关", "relevant"),
    ("不相关", "not_relevant"),
    ("已读", "already_read"),
    ("收藏", "save"),
]

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
        "news_items": "\n".join(_news_row(i, it) for i, it in enumerate(items, 1)),
        "paper_cards": "\n".join(_paper_card(i, it, cfg, user_email) for i, it in enumerate(items, 1)),
        "daily_summary": escape(daily_summary) if daily_summary else "（今日价值总结生成失败，请查看上方论文列表。）",
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


def _news_row(i: int, it: dict) -> str:
    p = it["paper"]
    return f"""      <tr>
        <td class="num">{i}</td>
        <td>
          <div class="news-head">{_badge(it.get("category", "Reference"))}
            <a class="title-link" href="{escape(p.url)}">{escape(p.title)}</a></div>
          <div class="news">{escape(it["news"])}</div>
          <div class="meta">{escape(p.journal)} · {escape(p.date)}</div>
        </td>
      </tr>"""


def _feedback_row(user_email: str, paper_id: int, cfg: dict) -> str:
    """卡片底部反馈链接：点击生成回信草稿，主题带 [FB] token 供 collector 解析。"""
    links = []
    for label, value in _FEEDBACK_CHOICES:
        subject = quote(f"[FB] u={user_email} p={paper_id} v={value}")
        body = quote("原因（可选）：")
        href = f"mailto:{cfg['feedback_email']}?subject={subject}&body={body}"
        links.append(f'<a href="{href}">{label}</a>')
    return f'<div class="meta feedback">反馈：{" · ".join(links)}（点击后发送回信即可）</div>'


def _paper_card(i: int, it: dict, cfg: dict, user_email: str = "") -> str:
    p = it["paper"]
    head = f'Rank {i} {_badge(it.get("category", "Reference"))}'
    if it.get("score") is not None:
        head += f' · {it["score"]} 分'
    rows = [
        f'<div class="card-head">{head}</div>',
        f'<div class="card-title">{escape(p.title)}</div>',
    ]
    if cfg["show_translation"] and it.get("title_zh"):
        rows.append(f'<div class="card-title-zh">{escape(it["title_zh"])}</div>')
    rows.append(f'<div class="meta">{escape(p.authors)}</div>')
    rows.append(f'<div class="meta">{escape(p.journal)} · {escape(p.date)}</div>')
    if cfg["show_doi"]:
        rows.append(f'<div class="meta">DOI: {escape(p.doi or "—")}</div>')
    rows.append(f'<div class="meta"><a href="{escape(p.url)}">{escape(p.url)}</a></div>')
    if cfg["show_keywords"]:
        keywords = "、".join(p.keywords) or "—"
        rows.append(f'<div class="meta">Keywords: {escape(keywords)}</div>')
    if user_email and cfg["feedback_email"] and it.get("paper_id"):
        rows.append(_feedback_row(user_email, it["paper_id"], cfg))
    if it.get("reason"):
        rows.append(f'<div class="reason"><span class="abs-label">推荐理由</span>{escape(it["reason"])}</div>')
    if cfg["show_translation"]:
        # 只展示中文四段结构化摘要（为空的段不渲染），不再显示英文摘要
        for key, label in _SECTION_LABELS:
            if it.get(key):
                rows.append(f'<div class="abstract"><span class="abs-label">{label}</span>{escape(it[key])}</div>')
    else:
        rows.append(f'<div class="abstract"><span class="abs-label">Abstract</span>{escape(p.abstract or "（无摘要）")}</div>')
    body = "\n      ".join(rows)
    return f'    <div class="card">\n      {body}\n    </div>'
