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
        "abstract_zh": "中文摘要内容",
        "category": "Must Read",
    }


def build(items, summary="今日趋势总结。", config=None):
    return build_digest_html("轩亚冰", "2025-07-22", items, summary, config)


def test_digest_contains_three_parts():
    html = build([make_item()])
    assert "今日论文新闻摘要" in html
    assert "论文详细信息卡片" in html
    assert "今日推荐文献价值总结" in html
    assert "今日趋势总结。" in html


def test_digest_contains_full_card_fields():
    html = build([make_item()])
    for token in ("轩亚冰", "Must Read", "中文标题", "中文摘要内容", "Zhang Wei",
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
    assert "中文摘要内容" not in html


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
    assert "推荐理由" not in html


def _build_with_feedback(item, user_email="a@x.com", config=None):
    cfg = {"feedback_email": "bot@x.com", **(config or {})}
    return build_digest_html("轩亚冰", "2025-07-22", [item], "总结。", cfg,
                             user_email=user_email)


def test_feedback_links_rendered_with_mailto_token():
    item = {**make_item(), "paper_id": 7}
    html = _build_with_feedback(item)
    assert "mailto:bot@x.com?subject=" in html
    # 主题经 URL 编码：[FB]→%5BFB%5D，=→%3D
    assert "%5BFB%5D" in html and "p%3D7" in html and "v%3Dsave" in html
    assert "不相关" in html and "收藏" in html


def test_feedback_links_absent_without_user_email():
    item = {**make_item(), "paper_id": 7}
    html = _build_with_feedback(item, user_email="")
    assert "mailto:" not in html


def test_feedback_links_absent_without_paper_id_or_feedback_email():
    # dry-run 未入库（无 paper_id）或未配 feedback_email 时不渲染反馈行
    assert "mailto:" not in _build_with_feedback(make_item())
    item = {**make_item(), "paper_id": 7}
    html = build_digest_html("轩亚冰", "2025-07-22", [item], "总结。", {},
                             user_email="a@x.com")
    assert "mailto:" not in html
