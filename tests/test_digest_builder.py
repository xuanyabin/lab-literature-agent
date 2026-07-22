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
    return {"paper": paper, "analysis": {}, "news": "为解决X问题，作者利用Y方法，发现Z机制。"}


def test_digest_contains_core_sections():
    html = build_digest_html("轩亚冰", "2025-07-22", [make_item()])
    assert "Research News Digest" in html
    assert "Paper Cards" in html
    assert "轩亚冰" in html
    assert "为解决X问题" in html
    assert "https://pubmed.ncbi.nlm.nih.gov/40123456/" in html


def test_digest_escapes_html():
    html = build_digest_html("轩亚冰", "2025-07-22", [make_item()])
    assert "<Apis>" not in html
    assert "&lt;Apis&gt;" in html
