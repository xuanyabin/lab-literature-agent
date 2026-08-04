import hashlib
import hmac
from pathlib import Path

from mailer.digest_builder import build_digest_html, build_page_html
from sources.paper import Paper


def make_item():
    paper = Paper(
        title="Atlas of <Apis> & friends",
        abstract="Abstract with <tags>.",
        authors="Zhang Wei",
        journal="Nature Communications",
        date="2025-07-16",
        doi="10.1038/x",
        url="https://pubmed.ncbi.nlm.nih.gov/40123456/",
        keywords=["snRNA-seq"],
    )
    return {
        "paper": paper,
        "analysis": {},
        "news": "为解决X问题，作者利用Y方法，发现Z机制。",
        "title_zh": "中文标题",
        "background": "背景内容",
        "methods": "方法内容",
        "results": "结果内容",
        "significance": "意义内容",
        "category": "Must Read",
    }


def build(items, summary="今日趋势总结。", config=None):
    return build_digest_html("轩亚冰", "2025-07-22", items, summary, config)


def test_digest_merged_card_and_summary_parts():
    html = build([make_item()])
    assert "今日论文（1 篇）" in html
    assert "今日推荐文献价值总结" in html
    assert "今日趋势总结。" in html
    # Part 1/2 已合并为卡片：不再有独立新闻摘要表与详情卡片两个区块
    assert "今日论文新闻摘要" not in html
    assert "论文详细信息卡片" not in html


def test_card_collapsible_details_and_title_appears_once():
    html = build([make_item()])
    assert '<details class="card-details" open>' in html
    assert "详情 · 作者 / 摘要 / 推荐理由" in html
    # 合并后标题只出现一次（旧版新闻表与详情卡各出现一次）
    assert html.count("Atlas of &lt;Apis&gt; &amp; friends") == 1


def test_daily_template_keeps_native_details_marker_for_wps():
    template = Path("templates/daily_digest.html").read_text(encoding="utf-8")
    assert "display: list-item" in template
    assert "::-webkit-details-marker" not in template
    assert "list-style: none" not in template


def test_digest_contains_full_card_fields():
    html = build([make_item()])
    for token in ("轩亚冰", "Must Read", "中文标题", "背景内容", "方法内容",
                  "结果内容", "意义内容", "Zhang Wei",
                  "Nature Communications", "10.1038/x", "snRNA-seq",
                  "为解决X问题", "https://pubmed.ncbi.nlm.nih.gov/40123456/"):
        assert token in html


def test_digest_escapes_html():
    html = build([make_item()])
    assert "<Apis>" not in html
    assert "&lt;Apis&gt;" in html
    assert "<tags>" not in html


def test_translation_hidden_when_config_off():
    html = build([make_item()], config={"show_translation": False})
    assert "中文标题" not in html
    assert "背景内容" not in html
    # 关闭翻译时回退为英文摘要
    assert "Abstract with &lt;tags&gt;." in html


def test_four_sections_shown_and_english_abstract_hidden():
    html = build([make_item()])
    for label in ("背景", "研究方法", "研究结果", "研究意义"):
        assert f'<span class="abs-label">{label}</span>' in html
    # 开启翻译时只展示中文摘要，不再显示英文摘要
    assert "Abstract with" not in html


def test_empty_section_not_rendered():
    item = {**make_item(), "background": "", "results": ""}
    html = build([item])
    assert '<span class="abs-label">背景</span>' not in html
    assert '<span class="abs-label">研究结果</span>' not in html
    assert '<span class="abs-label">研究方法</span>' in html
    assert '<span class="abs-label">研究意义</span>' in html


def test_keywords_and_doi_hidden_when_config_off():
    html = build([make_item()], config={"show_keywords": False, "show_doi": False})
    assert "snRNA-seq" not in html
    assert "10.1038/x" not in html


def test_reason_and_score_shown_when_present():
    item = {**make_item(), "reason": "与你关注的蜜蜂单细胞研究直接相关", "score": 88}
    html = build([item])
    assert "推荐理由" in html
    assert "与你关注的蜜蜂单细胞研究直接相关" in html
    assert "88 分" in html


def test_reason_absent_when_empty():
    html = build([make_item()])
    # 折叠按钮文案含"推荐理由"四字，精确断言推荐理由内容块未渲染
    assert '<span class="abs-label">推荐理由</span>' not in html


def _build_with_feedback(items, user_email="a@x.com", config=None):
    cfg = {"feedback_email": "bot@x.com", **(config or {})}
    return build_digest_html("轩亚冰", "2025-07-22", items, "总结。", cfg,
                             user_email=user_email)


def test_feedback_block_single_mailto_with_date_token():
    html = _build_with_feedback([make_item()])
    assert "一键反馈" in html
    # 整封邮件只有一个反馈邮件链接（B6：批量反馈替代逐篇链接）
    assert html.count("mailto:") == 1
    assert "mailto:bot@x.com?subject=" in html
    # 主题经 URL 编码：[FB]→%5BFB%5D，=→%3D；批量格式带日期、不带论文 id
    assert "%5BFB%5D" in html and "d%3D2025-07-22" in html
    assert "p%3D" not in html


def test_feedback_block_prefills_numbered_lines():
    html = _build_with_feedback([make_item(), make_item()])
    # 正文预填两位编号行（"01: " 经 URL 编码为 01%3A）
    assert "01%3A" in html and "02%3A" in html


def test_feedback_block_absent_without_user_email_or_feedback_email():
    html = _build_with_feedback([make_item()], user_email="")
    assert "mailto:" not in html
    assert "一键反馈" not in html
    # 未配 feedback_email 时同样不渲染
    html = build_digest_html("轩亚冰", "2025-07-22", [make_item()], "总结。", {},
                             user_email="a@x.com")
    assert "mailto:" not in html
    assert "一键反馈" not in html


OVERVIEW = {"days": 1, "pool_total": 132, "matched": 27,
            "pushed": 8, "must_read": 2, "important": 3, "reference": 3}


def test_overview_block_rendered_yesterday():
    html = build_digest_html("轩亚冰", "2025-07-22", [make_item()], "总结。", None,
                             overview=OVERVIEW)
    assert "昨日全库新增 132 篇" in html
    assert "命中您的关键词 27 篇" in html
    assert "本次推送 8 篇（必读 2 · 重要 3 · 参考 3）" in html


def test_overview_block_rendered_multi_days():
    html = build_digest_html("轩亚冰", "2025-07-22", [make_item()], "总结。", None,
                             overview={**OVERVIEW, "days": 3})
    assert "过去 3 天全库新增 132 篇" in html
    assert "昨日全库新增" not in html  # 多日时不使用"昨日"措辞


def test_overview_block_absent_when_none():
    html = build([make_item()])
    assert 'class="overview"' not in html
    assert "全库新增" not in html


def test_star_links_mailto_in_each_card():
    item = {**make_item(), "paper_id": 42}
    html = _build_with_feedback([item])
    assert 'class="stars"' in html
    # ⭐1-5 五个 mailto 链接，主题预填 [FB] u=<邮箱> p=<论文id> v=<星级>（URL 编码）
    for n in (1, 2, 3, 4, 5):
        assert (f'mailto:bot@x.com?subject=%5BFB%5D%20u%3Da%40x.com%20p%3D42%20v%3D{n}'
                in html)
    assert "点击后发送邮件即完成反馈" in html


def test_stars_outside_details():
    # 五星反馈在折叠区外：不展开详情也能直接点击
    item = {**make_item(), "paper_id": 42}
    html = _build_with_feedback([item])
    assert html.index("</details>") < html.index('class="stars"')


def test_stars_absent_without_paper_id():
    html = _build_with_feedback([make_item()])  # dry-run 情形：item 无 paper_id
    assert 'class="stars"' not in html


def test_stars_absent_without_feedback_email():
    html = build_digest_html("轩亚冰", "2025-07-22", [{**make_item(), "paper_id": 42}],
                             "总结。", {}, user_email="a@x.com")
    assert 'class="stars"' not in html
    assert "mailto:" not in html  # 未配 feedback_email：星标与 Part 3 批量入口都不渲染


def _build_with_webhook(items, user_email="a@x.com", config=None):
    cfg = {"feedback_email": "bot@x.com",
           "feedback_webhook_url": "https://fb.workers.dev",
           "feedback_secret": "s3cret", **(config or {})}
    return build_digest_html("轩亚冰", "2025-07-22", items, "总结。", cfg,
                             user_email=user_email)


def _webhook_sig(user_email, paper_id, value, secret="s3cret"):
    """与 worker/feedback.js 一致的本地签名：HMAC-SHA256 hex 前 16 位。"""
    msg = f"{user_email}|{paper_id}|{value}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def test_star_links_webhook_one_click_when_configured():
    item = {**make_item(), "paper_id": 42}
    html = _build_with_webhook([item])
    assert 'class="stars"' in html
    # webhook 优先：星标不再用 mailto（全文唯一 mailto 是 Part 3 批量入口）
    assert html.count("mailto:") == 1
    assert "p%3D42" not in html
    for n in (1, 2, 3, 4, 5):
        sig = _webhook_sig("a@x.com", 42, n)
        assert f"https://fb.workers.dev/fb?u=a%40x.com&p=42&v={n}&s={sig}" in html
    assert "点击即完成反馈" in html


def test_star_links_webhook_signature_matches_hmac_spec():
    # 签名规格：HMAC-SHA256(key=FEEDBACK_SECRET, msg="邮箱|论文id|星级") hex 前 16 位
    item = {**make_item(), "paper_id": 7}
    html = _build_with_webhook([item], user_email="bob@lab.org")
    sig = _webhook_sig("bob@lab.org", 7, 5)
    assert len(sig) == 16 and all(c in "0123456789abcdef" for c in sig)
    assert f"u=bob%40lab.org&p=7&v=5&s={sig}" in html


def test_star_links_fall_back_to_mailto_when_webhook_incomplete():
    item = {**make_item(), "paper_id": 42}
    # 只配 URL 没配密钥 → 降级 mailto（现有行为）
    html = _build_with_webhook([item], config={"feedback_secret": ""})
    assert "/fb?u=" not in html
    assert ("mailto:bot@x.com?subject=%5BFB%5D%20u%3Da%40x.com%20p%3D42%20v%3D1"
            in html)
    assert "点击后发送邮件即完成反馈" in html


# ---------- 瘦身邮件（pages_base_url 已配置）与网页版完整报告 ----------

WEB_URL = "https://xuanyabin.github.io/lab-literature-agent/daily/2025-07-22/user001.html"


def _build_slim(items, web_url=WEB_URL, **kwargs):
    return build_digest_html("轩亚冰", "2025-07-22", items, "今日趋势总结。", None,
                             web_url=web_url, **kwargs)


def test_slim_email_drops_details_stars_part2_part3():
    html = _build_slim([{**make_item(), "paper_id": 42}], user_email="a@x.com")
    assert "<details" not in html
    assert 'class="stars"' not in html
    assert "今日推荐文献价值总结" not in html
    assert "一键反馈" not in html and "mailto:" not in html
    assert "今日趋势总结。" not in html


def test_slim_email_links_to_web_page_and_keeps_rows():
    html = _build_slim([make_item()], overview=OVERVIEW)
    assert "展开详情 · 网页版完整报告" in html and f'href="{WEB_URL}"' in html
    assert "昨日全库新增 132 篇" in html
    assert 'class="badge cat-must"' in html
    assert ('<a class="title-link" href="https://pubmed.ncbi.nlm.nih.gov/40123456/">'
            "Atlas of &lt;Apis&gt; &amp; friends</a>") in html
    assert "为解决X问题" in html
    assert "Nature Communications · 2025-07-16" in html


def _build_page(items, user_email="a@x.com", config=None, **kwargs):
    cfg = {"feedback_email": "bot@x.com", **(config or {})}
    return build_page_html("轩亚冰", "2025-07-22", items, "今日趋势总结。", cfg,
                           user_email=user_email, **kwargs)


def test_page_contains_details_summary_and_signed_star_urls():
    item = {**make_item(), "paper_id": 42}
    cfg = {"feedback_webhook_url": "https://fb.workers.dev", "feedback_secret": "s3cret"}
    html = _build_page([item], config=cfg, overview=OVERVIEW)
    assert '<details class="card-details">' in html
    assert '<details class="card-details" open>' not in html  # 网页版默认折叠
    assert "背景内容" in html and "Zhang Wei" in html
    assert "今日推荐文献价值总结" in html and "今日趋势总结。" in html
    assert "昨日全库新增 132 篇" in html
    for n in (1, 2, 3, 4, 5):
        sig = _webhook_sig("a@x.com", 42, n)
        assert (f'data-url="https://fb.workers.dev/fb?u=a%40x.com&amp;p=42&amp;v={n}&amp;s={sig}"'
                in html)
    assert "format=json" in html and "已记录" in html and "请重试" in html


def test_page_keyword_entry_mailto():
    html = _build_page([{**make_item(), "paper_id": 42}])
    assert "新增关键词" in html
    assert "mailto:bot@x.com?subject=%5BFB%5D" in html and "d%3D2025-07-22" in html


def _keyword_sig(user_email, digest_date, secret="s3cret"):
    """与 worker/feedback.js /kw 一致的本地签名：msg 为 "<邮箱>|<日期>"。"""
    msg = f"{user_email}|{digest_date}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def test_page_keyword_entry_webhook_signed_url():
    """webhook 配置时渲染页内输入框 + 提交按钮（签名 /kw URL 嵌入 data-url），
    不再渲染 mailto 降级入口。"""
    cfg = {"feedback_webhook_url": "https://fb.workers.dev", "feedback_secret": "s3cret"}
    html = _build_page([{**make_item(), "paper_id": 42}], config=cfg)
    sig = _keyword_sig("a@x.com", "2025-07-22")
    assert 'class="kw-input"' in html and 'class="feedback-btn kw-submit"' in html
    assert (f'data-url="https://fb.workers.dev/kw?u=a%40x.com&amp;d=2025-07-22&amp;s={sig}"'
            in html)
    assert "&k=" in html and "已提交，次日检索生效" in html and "提交失败，请重试" in html
    assert "mailto:bot@x.com?subject=%5BFB%5D" not in html


def test_page_keyword_entry_webhook_without_secret_falls_back_to_mailto():
    """webhook 只配了一半（缺 secret）时按未配置处理，降级 mailto。"""
    html = _build_page([{**make_item(), "paper_id": 42}],
                       config={"feedback_webhook_url": "https://fb.workers.dev"})
    assert "mailto:bot@x.com?subject=%5BFB%5D" in html
    assert 'class="kw-submit"' not in html


def test_page_keyword_entry_absent_without_feedback_email():
    html = build_page_html("轩亚冰", "2025-07-22", [make_item()], "总结。", {},
                           user_email="a@x.com")
    assert "<h2>新增关键词</h2>" not in html
    assert 'class="kw-submit"' not in html and "mailto:" not in html


def test_page_stars_fall_back_to_mailto_without_webhook():
    html = _build_page([{**make_item(), "paper_id": 42}])
    assert 'class="stars"' in html and 'class="star-btn"' not in html
    assert "mailto:bot@x.com?subject=%5BFB%5D%20u%3Da%40x.com%20p%3D42%20v%3D1" in html


def test_page_stars_absent_without_paper_id():
    html = _build_page([make_item()], config={"feedback_webhook_url": "https://fb.workers.dev",
                                              "feedback_secret": "s3cret"})
    assert 'class="star-btn"' not in html and 'class="stars"' not in html
