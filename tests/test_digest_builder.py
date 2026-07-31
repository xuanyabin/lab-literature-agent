from mailer.digest_builder import build_digest_html
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
    assert '<details class="card-details">' in html
    assert "展开详情" in html
    # 合并后标题只出现一次（旧版新闻表与详情卡各出现一次）
    assert html.count("Atlas of &lt;Apis&gt; &amp; friends") == 1


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
