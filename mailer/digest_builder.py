"""生成每日文献推荐 HTML 邮件（模板在 templates/daily_digest.html）。"""

import html
from pathlib import Path
from string import Template

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "daily_digest.html"


def build_digest_html(user_name: str, digest_date: str, items: list[dict]) -> str:
    """items: [{"paper": Paper, "analysis": dict, "news": str}, ...]"""
    news_rows = "\n".join(_news_row(i, it) for i, it in enumerate(items, 1))
    cards = "\n".join(_paper_card(i, it) for i, it in enumerate(items, 1))
    template = Template(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.safe_substitute(
        user_name=html.escape(user_name),
        date=html.escape(digest_date),
        count=len(items),
        news_items=news_rows,
        paper_cards=cards,
    )


def _news_row(i: int, it: dict) -> str:
    p = it["paper"]
    return f"""      <tr>
        <td class="num">{i}</td>
        <td>
          <div class="title"><a href="{html.escape(p.url)}">{html.escape(p.title)}</a></div>
          <div class="news">{html.escape(it["news"])}</div>
          <div class="meta">{html.escape(p.journal)} · {html.escape(p.date)}</div>
        </td>
      </tr>"""


def _paper_card(i: int, it: dict) -> str:
    p = it["paper"]
    keywords = "、".join(p.keywords) or "—"
    doi = p.doi or "—"
    abstract = p.abstract or "（无摘要）"
    return f"""    <div class="card">
      <div class="card-title">{i}. {html.escape(p.title)}</div>
      <div class="meta">{html.escape(p.authors)}</div>
      <div class="meta">{html.escape(p.journal)} · {html.escape(p.date)} · DOI: {html.escape(doi)}</div>
      <div class="meta"><a href="{html.escape(p.url)}">{html.escape(p.url)}</a></div>
      <div class="abstract">{html.escape(abstract)}</div>
      <div class="meta">Keywords: {html.escape(keywords)}</div>
    </div>"""
