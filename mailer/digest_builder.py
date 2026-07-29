"""组装每日文献情报邮件的各段内容（总览 / 新闻摘要 / 详细卡片 / 价值总结 / 反馈入口），渲染交给 template_renderer。

反馈（两种模式，按配置自动切换）：
- HTTP 模式（config 含 feedback_base_url 与 feedback_secret）：每张卡片底部内嵌
  ⭐1-5 星级链接，接收人点击即由 feedback/server.py 落库并显示"已反馈"页；
  Part 4 收窄为一个"新增关注关键词"入口（/kw 表单）。链接带 HMAC token 防伪造。
- 回退模式（未配置 base_url/secret）：整封邮件一个"一键反馈"mailto（Part 4），
  回信主题带 [FB] token 与日期，正文按编号标注 1-5 星、以 "+" 开头的行是新增关键词，
  由 python -m feedback 收集学习。
两种模式都要求调用方提供 user_email 且 items 非空。
"""

from pathlib import Path
from urllib.parse import quote

import yaml

from feedback.tokens import make_token
from mailer.template_renderer import escape, render

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_EMAIL_CONFIG = BASE_DIR / "config" / "email.yaml"

_DEFAULT_CONFIG = {
    "daily_paper_number": 15,
    "show_translation": True,
    "show_keywords": True,
    "show_doi": True,
    "feedback_email": "",
    "feedback_base_url": "",
    "feedback_secret": "",
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
        "news_items": "\n".join(_news_row(i, it) for i, it in enumerate(items, 1)),
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


def _http_mode(cfg: dict) -> bool:
    """配置了 feedback_base_url + feedback_secret 时走 HTTP 五星反馈服务。"""
    return bool(cfg.get("feedback_base_url") and cfg.get("feedback_secret"))


def _feedback_block(user_email: str, digest_date: str, count: int, cfg: dict) -> str:
    """Part 4 反馈入口：HTTP 模式收窄为关键词表单链接；回退模式为批量标注 mailto。"""
    if not user_email or count <= 0:
        return ""
    if _http_mode(cfg):
        base = cfg["feedback_base_url"].rstrip("/")
        token = make_token(cfg["feedback_secret"], user_email, "kw")
        href = f"{base}/kw?u={quote(user_email)}&t={token}"
        return f"""    <h2>Part 4 · 反馈与关键词</h2>
    <p class="section-sub">在每张卡片下方点 ⭐ 即完成反馈（无需回信）</p>
    <div class="feedback-box">
      有希望我们持续跟踪的新方向？<br>
      <a class="feedback-btn" href="{href}">新增关注关键词</a>
    </div>"""
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
    return f"""    <h2>Part 4 · 一键反馈</h2>
    <p class="section-sub">只回一封邮件 · 按编号标注星级，帮助我们越推越准</p>
    <div class="feedback-box">
      点击下面的按钮打开反馈邮件草稿，在编号后填 1-5 星（只评想评的即可），发送即完成：<br>
      <a class="feedback-btn" href="{href}">一键标注今日 {count} 篇论文</a>
    </div>"""


_RATING_LABELS = {1: "完全不相关", 2: "不太相关", 3: "一般", 4: "比较重要", 5: "非常重要"}


def _star_row(user_email: str, paper_id, cfg: dict) -> str:
    """卡片底部 ⭐1-5 反馈链接（HTTP 模式专用）；缺配置或 paper_id 时不渲染。"""
    if not (_http_mode(cfg) and user_email and paper_id):
        return ""
    base = cfg["feedback_base_url"].rstrip("/")
    secret = cfg["feedback_secret"]
    pid = str(paper_id)
    links = []
    for n in (1, 2, 3, 4, 5):
        token = make_token(secret, user_email, pid, str(n))
        href = f"{base}/fb?u={quote(user_email)}&p={pid}&v={n}&t={token}"
        links.append(f'<a class="star" href="{href}" title="{_RATING_LABELS[n]}">⭐{n}</a>')
    return ('<div class="stars"><span class="abs-label">重要性反馈</span>'
            + " ".join(links)
            + '<span class="stars-hint">点击即提交 · ⭐1 完全不相关 – ⭐5 非常重要</span></div>')


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
    if it.get("reason"):
        rows.append(f'<div class="reason"><span class="abs-label">推荐理由</span>{escape(it["reason"])}</div>')
    if cfg["show_translation"]:
        # 只展示中文四段结构化摘要（为空的段不渲染），不再显示英文摘要
        for key, label in _SECTION_LABELS:
            if it.get(key):
                rows.append(f'<div class="abstract"><span class="abs-label">{label}</span>{escape(it[key])}</div>')
    else:
        rows.append(f'<div class="abstract"><span class="abs-label">Abstract</span>{escape(p.abstract or "（无摘要）")}</div>')
    star = _star_row(user_email, it.get("paper_id"), cfg)
    if star:
        rows.append(star)
    body = "\n      ".join(rows)
    return f'    <div class="card">\n      {body}\n    </div>'
