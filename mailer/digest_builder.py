"""组装每日文献情报邮件的三段内容（新闻摘要 / 详细卡片 / 价值总结），渲染交给 template_renderer。"""

from pathlib import Path

import yaml

from mailer.template_renderer import escape, render

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_EMAIL_CONFIG = BASE_DIR / "config" / "email.yaml"

_DEFAULT_CONFIG = {
    "daily_paper_number": 15,
    "show_translation": True,
    "show_keywords": True,
    "show_doi": True,
}

_CATEGORY_CLASS = {
    "Must Read": "cat-must",
    "Important": "cat-important",
    "Reference": "cat-reference",
    "Ignore": "cat-ignore",
}


def load_email_config(path: Path = DEFAULT_EMAIL_CONFIG) -> dict:
    """读取 config/email.yaml，缺省字段回退到默认值。"""
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {**_DEFAULT_CONFIG, **cfg}


def build_digest_html(user_name: str, digest_date: str, items: list[dict],
                      daily_summary: str, config: dict | None = None) -> str:
    """items: [{"paper": Paper, "analysis": dict, "news": str,
               "title_zh": str, "abstract_zh": str, "category": str}, ...]（按推荐排序）"""
    cfg = {**_DEFAULT_CONFIG, **(config or {})}
    context = {
        "user_name": escape(user_name),
        "date": escape(digest_date),
        "count": str(len(items)),
        "news_items": "\n".join(_news_row(i, it) for i, it in enumerate(items, 1)),
        "paper_cards": "\n".join(_paper_card(i, it, cfg) for i, it in enumerate(items, 1)),
        "daily_summary": escape(daily_summary) if daily_summary else "（今日价值总结生成失败，请查看上方论文列表。）",
    }
    return render("daily_digest.html", context)


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


def _paper_card(i: int, it: dict, cfg: dict) -> str:
    p = it["paper"]
    rows = [
        f'<div class="card-head">Rank {i} {_badge(it.get("category", "Reference"))}</div>',
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
    rows.append(f'<div class="abstract"><span class="abs-label">Abstract</span>{escape(p.abstract or "（无摘要）")}</div>')
    if cfg["show_translation"] and it.get("abstract_zh"):
        rows.append(f'<div class="abstract"><span class="abs-label">中文摘要</span>{escape(it["abstract_zh"])}</div>')
    body = "\n      ".join(rows)
    return f'    <div class="card">\n      {body}\n    </div>'
