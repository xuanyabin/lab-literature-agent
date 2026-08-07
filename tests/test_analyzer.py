import json

from processing.analyzer import EMPTY_ANALYSIS, analyze_paper
from sources.paper import Paper


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def make_paper():
    return Paper(
        title="Single-cell atlas of the honeybee brain",
        abstract="We used snRNA-seq to profile Apis mellifera brains.",
        authors="Zhang Wei",
        journal="Nature Communications",
        date="2025-07-16",
        doi="10.1038/x",
        url="https://pubmed.ncbi.nlm.nih.gov/40123456/",
        keywords=["snRNA-seq"],
    )


def test_analyze_parses_json_and_prompt_contains_metadata():
    reply = json.dumps({
        "field": "昆虫神经科学",
        "problem": "p", "solution": "s", "finding": "f",
        "methods": ["snRNA-seq"], "organisms": ["Apis mellifera"],
    }, ensure_ascii=False)
    llm = FakeLLM(reply)
    result = analyze_paper(make_paper(), llm)
    assert result["field"] == "昆虫神经科学"
    assert result["methods"] == ["snRNA-seq"]
    assert "Single-cell atlas" in llm.prompts[0]
    assert "snRNA-seq to profile" in llm.prompts[0]


def test_analyze_strips_markdown_fence():
    llm = FakeLLM('```json\n{"field": "x", "methods": []}\n```')
    assert analyze_paper(make_paper(), llm)["field"] == "x"


def test_analyze_invalid_json_falls_back_to_empty():
    llm = FakeLLM("这不是JSON")
    assert analyze_paper(make_paper(), llm) == EMPTY_ANALYSIS


# ---------- paper_type：三值解析、非法值回退、PubMed Review 元数据优先 ----------

def _llm_with_type(paper_type):
    return FakeLLM(json.dumps({"field": "x", "paper_type": paper_type}, ensure_ascii=False))


def test_paper_type_valid_values_kept():
    for value in ("方法学", "研究", "综述"):
        assert analyze_paper(make_paper(), _llm_with_type(value))["paper_type"] == value


def test_paper_type_invalid_value_falls_back_to_empty():
    assert analyze_paper(make_paper(), _llm_with_type("Letter"))["paper_type"] == ""
    # LLM 未输出该字段时同样为空
    assert analyze_paper(make_paper(), FakeLLM('{"field": "x"}'))["paper_type"] == ""


def test_paper_type_pubmed_review_metadata_wins():
    paper = make_paper()
    paper.publication_types = ["Journal Article", "Review"]
    # LLM 说"研究"也被元数据覆盖为"综述"
    assert analyze_paper(paper, _llm_with_type("研究"))["paper_type"] == "综述"


def test_paper_type_non_review_metadata_uses_llm():
    paper = make_paper()
    paper.publication_types = ["Journal Article"]
    assert analyze_paper(paper, _llm_with_type("方法学"))["paper_type"] == "方法学"


# ---------- category / subcategory：合法保留、非法回退、缺字段回退 ----------

def _llm_with_taxonomy(category, subcategory):
    payload = {"field": "x"}
    if category is not None:
        payload["category"] = category
    if subcategory is not None:
        payload["subcategory"] = subcategory
    return FakeLLM(json.dumps(payload, ensure_ascii=False))


def test_taxonomy_valid_pair_kept():
    result = analyze_paper(make_paper(),
                           _llm_with_taxonomy("cellular_spatial_biology", "spatial_omics"))
    assert result["category"] == "cellular_spatial_biology"
    assert result["subcategory"] == "spatial_omics"


def test_taxonomy_empty_pair_kept_as_unclassified():
    result = analyze_paper(make_paper(), _llm_with_taxonomy("", ""))
    assert result["category"] == "" and result["subcategory"] == ""


def test_taxonomy_invalid_category_falls_back():
    result = analyze_paper(make_paper(), _llm_with_taxonomy("genomics", "pangenomics"))
    assert result["category"] == "" and result["subcategory"] == ""


def test_taxonomy_subcategory_not_in_category_falls_back():
    # spatial_omics 不属于 genome_evolution_diversity
    result = analyze_paper(make_paper(),
                           _llm_with_taxonomy("genome_evolution_diversity", "spatial_omics"))
    assert result["category"] == "" and result["subcategory"] == ""


def test_taxonomy_only_one_field_falls_back():
    for cat, sub in (("genome_evolution_diversity", ""), ("", "pangenomics")):
        result = analyze_paper(make_paper(), _llm_with_taxonomy(cat, sub))
        assert result["category"] == "" and result["subcategory"] == ""


def test_taxonomy_missing_fields_falls_back():
    result = analyze_paper(make_paper(), FakeLLM('{"field": "x"}'))
    assert result["category"] == "" and result["subcategory"] == ""
