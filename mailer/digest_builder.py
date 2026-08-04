"""组装每日文献情报的邮件与网页版报告（总览 / 论文列表 / 价值总结 / 反馈入口），渲染交给 template_renderer。

两种投递形态（build_digest_html 按 web_url 是否提供二选一）：
- 瘦身邮件（配置 pages_base_url）：总览块 + 每篇一行（序号 / 徽章 / 标题链接 /
  news 一句话 / 期刊·日期）+ 底部"展开详情 · 网页版完整报告"按钮，详情迁移到网页版；
- 完整邮件（pages_base_url 未配置的降级形态，模板 daily_digest.html）：每篇一张
  卡片，<details open> 默认展示完整信息（作者 / 摘要 / 推荐理由），不支持交互的
  邮件客户端也能直接看到详情。

网页版完整报告（build_page_html，模板 daily_page.html，发布到 GitHub Pages）：
总览块 + 完整卡片（<details> 默认折叠，浏览器可展开收起）+ 今日价值总结 +
新增关键词入口（mailto 回信，"+" 开头的行由 python -m feedback 收集学习）。

反馈入口（⭐1-5 与新增关键词最终都由 python -m feedback 学习闭环消费）：
- 逐篇五星：卡片底部 ⭐1-5，两种模式按优先级取一——
  · webhook（feedback_webhook_url 与 feedback_secret 均配置）：指向 Cloudflare Worker
    （worker/feedback.js，/fb?u=<邮箱>&p=<论文id>&v=<星级>&s=<HMAC 签名>），
    Worker 校验签名后直写仓库 feedback_data/pending/。完整邮件里点击跳确认页；
    网页版里由页内 JS fetch（format=json 响应 + CORS）无感记录，签名 URL 在
    生成页面时逐星预算好嵌入 data-url；
  · mailto（feedback_email）：主题预填 "[FB] u=<用户邮箱> p=<论文id> v=<星级>"，
    点击拉起邮件草稿，发送后经 IMAP 收集（webhook 未配置时的降级通道）；
- 批量标注（仅完整邮件）：整封邮件一个"一键反馈"mailto（Part 3），回信主题带
  [FB] token 与日期，正文按编号标注 1-5 星、以 "+" 开头的行是新增关键词；
  网页版对应新增关键词入口 mailto（正文只保留 + 关键词行）。
两条路都要求调用方提供 user_email 且 items 非空。
"""

import hashlib
import hmac
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
    "feedback_webhook_url": "",
    "feedback_secret": "",
    "pages_base_url": "",  # GitHub Pages 根地址；空 = 降级为完整版邮件
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
                      user_email: str = "", overview: dict | None = None,
                      web_url: str = "") -> str:
    """items: [{"paper": Paper, "analysis": dict, "news": str, "title_zh": str,
               "background": str, "methods": str, "results": str,
               "significance": str, "category": str}, ...]（按推荐排序）
    overview: {"days", "pool_total", "matched", "pushed", "must_read",
               "important", "reference"}，为 None 时不渲染开头总览块。
    web_url 非空时渲染瘦身邮件（一句话摘要列表 + 网页版入口按钮）；
    为空时渲染完整版邮件（pages_base_url 未配置的降级形态）。"""
    cfg = {**_DEFAULT_CONFIG, **(config or {})}
    if web_url:
        context = {
            "user_name": escape(user_name),
            "date": escape(digest_date),
            "count": str(len(items)),
            "overview_block": _overview_block(overview) if overview else "",
            "paper_rows": "\n".join(_paper_row(i, it) for i, it in enumerate(items, 1)),
            "web_url": escape(web_url),
        }
        return render("daily_digest_slim.html", context)
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


def build_page_html(user_name: str, digest_date: str, items: list[dict],
                    daily_summary: str, config: dict | None = None,
                    user_email: str = "", overview: dict | None = None) -> str:
    """网页版完整报告（发布到 GitHub Pages）：总览块 + 完整卡片（详情默认折叠，
    卡片底部 ⭐1-5 页内无感反馈）+ 今日价值总结 + 新增关键词入口。
    items / overview 含义同 build_digest_html。"""
    cfg = {**_DEFAULT_CONFIG, **(config or {})}
    context = {
        "user_name": escape(user_name),
        "date": escape(digest_date),
        "count": str(len(items)),
        "overview_block": _overview_block(overview) if overview else "",
        "paper_cards": "\n".join(
            _paper_card(i, it, cfg, user_email, open_details=False,
                        star_row=_page_star_row(user_email, it.get("paper_id"), cfg))
            for i, it in enumerate(items, 1)),
        "daily_summary": escape(daily_summary) if daily_summary else "（今日价值总结生成失败，请查看上方论文列表。）",
        "keyword_entry": _keyword_entry(user_email, digest_date, cfg),
    }
    return render("daily_page.html", context)


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


def _paper_row(i: int, it: dict) -> str:
    """瘦身邮件的一行：序号 + 定级徽章 + 论文标题（链到原文）+ news 一句话概要 + 期刊·日期。"""
    p = it["paper"]
    rows = [f'<div class="row-title">{i}. {_badge(it.get("category", "Reference"))}'
            f'<a class="title-link" href="{escape(p.url)}">{escape(p.title)}</a></div>']
    if it.get("news"):
        rows.append(f'<div class="news">{escape(it["news"])}</div>')
    rows.append(f'<div class="meta">{escape(p.journal)} · {escape(p.date)}</div>')
    return '    <div class="row">\n      ' + "\n      ".join(rows) + '\n    </div>'


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


def _webhook_star_url(base_url: str, secret: str, user_email: str, paper_id, value: int) -> str:
    """星标一键反馈链接。签名：HMAC-SHA256(key=FEEDBACK_SECRET,
    msg="<邮箱>|<论文id>|<星级>") 取 hex 前 16 位——与 worker/feedback.js 的
    校验算法必须严格一致，改动需两侧同步。
    邮件里作为普通 <a> 跳转（Worker 返回 HTML 确认页）；网页版报告里逐星嵌入
    data-url 供页内 JS fetch（Worker 以 format=json / Accept: application/json
    返回 JSON 并带 CORS 头），URL 本体与签名两场景完全相同。"""
    msg = f"{user_email}|{paper_id}|{value}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]
    return (f"{base_url.rstrip('/')}/fb?u={quote(str(user_email))}"
            f"&p={paper_id}&v={value}&s={sig}")


def _star_row(user_email: str, paper_id, cfg: dict) -> str:
    """卡片底部 ⭐1-5 反馈链接：webhook（feedback_webhook_url + feedback_secret
    均配置，点击即完成反馈）优先，mailto（feedback_email，点击拉起预填邮件草稿，
    发送即完成反馈）降级；两者都未配置、缺 user_email 或 paper_id（dry-run）时不渲染。"""
    if not (user_email and paper_id):
        return ""
    webhook = bool(cfg["feedback_webhook_url"] and cfg["feedback_secret"])
    if not webhook and not cfg["feedback_email"]:
        return ""
    links = []
    for n in (1, 2, 3, 4, 5):
        if webhook:
            href = _webhook_star_url(cfg["feedback_webhook_url"], cfg["feedback_secret"],
                                     user_email, paper_id, n)
        else:
            subject = quote(f"[FB] u={user_email} p={paper_id} v={n}")
            href = f"mailto:{cfg['feedback_email']}?subject={subject}"
        links.append(f'<a class="star" href="{href}" title="{_RATING_LABELS[n]}">⭐{n}</a>')
    hint = "点击即完成反馈" if webhook else "点击后发送邮件即完成反馈"
    return ('<div class="stars"><span class="abs-label">重要性反馈</span>'
            + " ".join(links)
            + f'<span class="stars-hint">{hint} · ⭐1 完全不相关 – ⭐5 非常重要</span></div>')


def _page_star_row(user_email: str, paper_id, cfg: dict) -> str:
    """网页版 ⭐1-5：webhook 配置时渲染 JS fetch 五星按钮（签名 URL 逐星预算好
    嵌入 data-url，点击由 daily_page.html 的页内脚本无感记录，不跳页）；
    webhook 未配置时降级 _star_row 的 mailto 逻辑；缺 user_email 或
    paper_id（dry-run）时不渲染。"""
    if not (user_email and paper_id):
        return ""
    if not (cfg["feedback_webhook_url"] and cfg["feedback_secret"]):
        return _star_row(user_email, paper_id, cfg)
    buttons = []
    for n in (1, 2, 3, 4, 5):
        url = _webhook_star_url(cfg["feedback_webhook_url"], cfg["feedback_secret"],
                                user_email, paper_id, n)
        buttons.append(f'<button type="button" class="star-btn" data-url="{escape(url)}" '
                       f'title="{_RATING_LABELS[n]}">⭐{n}</button>')
    return ('<div class="stars"><span class="abs-label">重要性反馈</span>'
            + " ".join(buttons)
            + '<span class="stars-hint">点击即完成反馈 · ⭐1 完全不相关 – ⭐5 非常重要</span>'
            + '<span class="stars-status"></span></div>')


def _keyword_entry(user_email: str, digest_date: str, cfg: dict) -> str:
    """网页版新增关键词入口：mailto 回信（正文 "+" 开头的行由 python -m feedback
    收集学习；说明行沿用"如需新增关键词"前缀，collector 会过滤掉它）。
    未配 feedback_email 或缺 user_email 时不渲染。"""
    if not (user_email and cfg["feedback_email"]):
        return ""
    subject = quote(f"[FB] u={user_email} d={digest_date}")
    body = quote("\n".join([
        "如需新增关键词，请在下方 + 号后填写（每行一个，可用逗号分隔）：",
        "+",
    ]))
    href = f"mailto:{cfg['feedback_email']}?subject={subject}&body={body}"
    return f"""    <h2>新增关键词</h2>
    <p class="section-sub">有想看但没收到的方向？回信告诉我们，次日检索即生效</p>
    <div class="feedback-box">
      点击按钮打开邮件草稿，在 + 号后填写关键词（每行一个，可用逗号分隔），发送即完成：<br>
      <a class="feedback-btn" href="{href}">✉️ 新增关键词</a>
    </div>"""


def _paper_card(i: int, it: dict, cfg: dict, user_email: str = "",
                open_details: bool = True, star_row: str | None = None) -> str:
    """单篇论文卡片：标题 + 一句话新闻概要 + 期刊日期默认可见。
    <details> 区内放作者 / DOI / 关键词 / 推荐理由 / 中文四段摘要；
    open_details=True（完整邮件）默认展开，保证 WPS 等不支持展开交互的客户端
    也能看到完整详情；False（网页版）默认折叠，浏览器可展开收起。
    五星反馈在折叠区外，不展开也能直接点击；star_row 传入时替代默认
    _star_row（网页版的 JS 五星按钮），传 None 走邮件原有逻辑。"""
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
    rows.append(f'<details class="card-details"{" open" if open_details else ""}>'
                '<summary class="detail-toggle">详情 · 作者 / 摘要 / 推荐理由</summary>\n'
                + "\n".join(detail) + '\n</details>')

    star = _star_row(user_email, it.get("paper_id"), cfg) if star_row is None else star_row
    if star:
        rows.append(star)
    body = "\n      ".join(rows)
    return f'    <div class="card">\n      {body}\n    </div>'
