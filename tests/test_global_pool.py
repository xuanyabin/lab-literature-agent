"""sources.global_pool 的测试：词表合并 / 聚类缓存 / 分簇检索与去重（全 mock，无网络）。"""

import sources.global_pool as gp
from sources.paper import Paper


class FakeLLM:
    """记录调用次数，按预设输出依次返回或抛错。"""

    def __init__(self, outputs=()):
        self.outputs = list(outputs)
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _terms():
    return {"species": ["Apis"], "others": ["genomics", "scRNA-seq"]}


def _good_output():
    return '[{"topic": "蜜蜂基因组", "species": ["Apis"], "terms": ["genomics", "scRNA-seq"]}]'


# ---------- collect_global_terms ----------

def test_collect_global_terms_merges_expands_and_dedupes():
    users = [
        {"species": ["honeybee"], "aliases": {"honeybee": ["Apis mellifera"]},
         "research_interest": ["Insect Evolution"], "keywords": ["genomics"], "methods": []},
        {"species": ["Honeybee", "Bombus"], "research_interest": ["insect evolution"],
         "keywords": [], "methods": ["scRNA-seq"],
         "learned_terms": [("gut microbiome", 1.0), ("Genomics", 2.0)]},
    ]
    terms = gp.collect_global_terms(users)
    # aliases 展开；大小写去重保持顺序；learned_terms 进其余组
    assert terms["species"] == ["honeybee", "Apis mellifera", "Bombus"]
    assert terms["others"] == ["Insect Evolution", "genomics", "scRNA-seq", "gut microbiome"]


def test_collect_global_terms_includes_lab_topics_and_skips_bad_items():
    # lab_topics 并入其余组参与召回（无 lab_recall 时的兼容回退）；空串/None 被过滤
    users = [{"species": ["Apis", "", None], "lab_topics": ["Genome Evolution"],
              "research_interest": ["genomics"]}]
    terms = gp.collect_global_terms(users)
    assert terms == {"species": ["Apis"], "others": ["genomics", "Genome Evolution"]}


def test_collect_global_terms_prefers_lab_recall_over_lab_topics():
    # V5：有 lab_recall 时优先之；lab_topics 里的 rank_only 词不进召回检索式
    users = [{"species": [], "research_interest": [], "keywords": [], "methods": [],
              "lab_recall": ["eusociality"],
              "lab_topics": ["eusociality", "spatial transcriptomics"]}]
    terms = gp.collect_global_terms(users)
    assert terms["others"] == ["eusociality"]


def test_collect_global_terms_empty_lab_recall_not_falling_back():
    # V5：lab_recall 存在但为空时不能用 lab_topics 兜底（rank_only 会漏进召回）
    users = [{"species": [], "research_interest": [], "keywords": [], "methods": [],
              "lab_recall": [], "lab_topics": ["spatial transcriptomics"]}]
    terms = gp.collect_global_terms(users)
    assert terms["others"] == []


# ---------- build_cluster_query ----------

def test_build_cluster_query_flat_or_equal_weight():
    # 关键词等权：物种词与其他词同一层扁平 OR，不再有 AND 分组
    q = gp.build_cluster_query({"species": ["Apis", "Bombus"], "terms": ["genomics"]})
    assert q == '"Apis" OR "Bombus" OR "genomics"'
    assert "AND" not in q and "NOT" not in q


def test_build_cluster_query_without_species_flat_or():
    q = gp.build_cluster_query({"species": [], "terms": ["genomics", "scRNA-seq"]})
    assert q == '"genomics" OR "scRNA-seq"'
    assert "NOT" not in q


# ---------- cluster_terms ----------

def test_cluster_terms_writes_cache_then_hits_without_llm(tmp_path):
    cache = tmp_path / "_clusters.yaml"
    llm = FakeLLM([_good_output()])
    clusters = gp.cluster_terms(_terms(), llm, cache_path=cache)
    assert llm.calls == 1
    assert clusters[0]["topic"] == "蜜蜂基因组"
    assert cache.exists()
    # 词表哈希不变：直接命中缓存，不再调 LLM
    llm2 = FakeLLM()
    assert gp.cluster_terms(_terms(), llm2, cache_path=cache) == clusters
    assert llm2.calls == 0


def test_cluster_terms_hash_change_refreshes(tmp_path):
    cache = tmp_path / "_clusters.yaml"
    gp.cluster_terms(_terms(), FakeLLM([_good_output()]), cache_path=cache)
    new_terms = {"species": ["Mus musculus"], "others": ["genomics", "scRNA-seq"]}
    out = '[{"topic": "小鼠基因组", "species": ["Mus musculus"], "terms": ["genomics", "scRNA-seq"]}]'
    llm = FakeLLM([out])
    clusters = gp.cluster_terms(new_terms, llm, cache_path=cache)
    assert llm.calls == 1
    assert clusters[0]["topic"] == "小鼠基因组"


def test_cluster_terms_llm_error_falls_back_to_old_cache(tmp_path):
    cache = tmp_path / "_clusters.yaml"
    old = gp.cluster_terms(_terms(), FakeLLM([_good_output()]), cache_path=cache)
    # 词表变了需要刷新，但 LLM 抛错 → 沿用旧缓存
    llm = FakeLLM([RuntimeError("boom")])
    clusters = gp.cluster_terms({"species": ["Bombus"], "others": ["ecology"]},
                                llm, cache_path=cache)
    assert llm.calls == 1
    assert clusters == old


def test_cluster_terms_no_cache_falls_back_to_single_cluster(tmp_path):
    llm = FakeLLM([RuntimeError("boom")])
    clusters = gp.cluster_terms(_terms(), llm, cache_path=tmp_path / "none.yaml")
    assert clusters == [{"topic": "全部", "species": ["Apis"],
                         "terms": ["genomics", "scRNA-seq"]}]


def test_cluster_terms_incomplete_coverage_treated_as_failure(tmp_path):
    # LLM 输出缺了 scRNA-seq → 覆盖校验失败；无旧缓存 → 回退单簇且不写缓存
    bad = '[{"topic": "部分", "species": ["Apis"], "terms": ["genomics"]}]'
    llm = FakeLLM([bad])
    clusters = gp.cluster_terms(_terms(), llm, cache_path=tmp_path / "none.yaml")
    assert clusters == [{"topic": "全部", "species": ["Apis"],
                         "terms": ["genomics", "scRNA-seq"]}]
    assert not (tmp_path / "none.yaml").exists()


# ---------- fetch_global_pubmed ----------

def _paper(doi):
    return Paper(title=f"t-{doi}", abstract="", authors="", journal="J",
                 date="2026-07-22", doi=doi, url="", keywords=[])


def test_fetch_global_pubmed_flat_or_and_cross_cluster_dedupe(monkeypatch):
    searches = []

    def fake_search(query, days, retmax):
        searches.append(query)
        if "ecology" in query:
            return ["2", "3"]
        return ["1", "2"]

    monkeypatch.setattr(gp.pubmed, "search_pmids", fake_search)
    monkeypatch.setattr(gp.pubmed, "fetch_by_pmids",
                        lambda pmids: [_paper(f"10.1/{i}") for i in pmids])
    monkeypatch.setattr(gp.time, "sleep", lambda _: None)

    clusters = [
        {"topic": "蜜蜂", "species": ["Apis"], "terms": ["genomics"]},
        {"topic": "生态", "species": [], "terms": ["ecology"]},
    ]
    papers = gp.fetch_global_pubmed(clusters, days=1)
    # 扁平 OR：每簇只检索一次（无严格查询与降级重试）
    assert len(searches) == 2
    assert all(" AND " not in q and "NOT" not in q for q in searches)
    assert [p.doi for p in papers] == ["10.1/1", "10.1/2", "10.1/3"]  # 跨簇合并去重


def test_fetch_global_pubmed_cluster_error_continues(monkeypatch):
    def fake_search(query, days, retmax):
        if "bad" in query:
            raise ConnectionError("boom")
        return ["9"]

    monkeypatch.setattr(gp.pubmed, "search_pmids", fake_search)
    monkeypatch.setattr(gp.pubmed, "fetch_by_pmids",
                        lambda pmids: [_paper(f"10.1/{i}") for i in pmids])
    monkeypatch.setattr(gp.time, "sleep", lambda _: None)

    clusters = [{"topic": "坏簇", "species": [], "terms": ["bad"]},
                {"topic": "好簇", "species": [], "terms": ["good"]}]
    papers = gp.fetch_global_pubmed(clusters, days=1)
    assert [p.doi for p in papers] == ["10.1/9"]  # 单簇异常不影响其余簇


# ---------- fetch_global_pool 顶刊通道 ----------

def test_fetch_global_pool_merges_journal_channel(monkeypatch):
    monkeypatch.setattr(gp.pubmed, "search_pmids", lambda *_a, **_k: ["1"])
    monkeypatch.setattr(gp.pubmed, "fetch_by_pmids",
                        lambda pmids: [_paper(f"10.1/{i}") for i in pmids])
    monkeypatch.setattr(gp.biorxiv, "fetch_recent_global", lambda *_a, **_k: [])
    monkeypatch.setattr(gp, "cluster_terms", lambda terms, llm: [
        {"topic": "全部", "species": terms["species"], "terms": terms["others"]}])
    monkeypatch.setattr(gp.time, "sleep", lambda _: None)

    def fake_top(names, days, retmax):
        assert names == ["Nature"]
        return [_paper("10.1/1"), _paper("10.9/top")]  # 10.1/1 与关键词池撞 DOI

    monkeypatch.setattr(gp.top_journals, "fetch_top_journals", fake_top)
    users = [{"species": ["Apis"], "research_interest": [], "keywords": [], "methods": []}]
    papers = gp.fetch_global_pool(users, FakeLLM(), days=1,
                                  journal_channel={"names": ["Nature"], "retmax_per_journal": 5})
    # 撞 DOI 时关键词池版本优先，顶刊新论文并入
    assert [p.doi for p in papers] == ["10.1/1", "10.9/top"]


def test_fetch_global_pool_without_journal_channel(monkeypatch):
    monkeypatch.setattr(gp.pubmed, "search_pmids", lambda *_a, **_k: ["1"])
    monkeypatch.setattr(gp.pubmed, "fetch_by_pmids",
                        lambda pmids: [_paper(f"10.1/{i}") for i in pmids])
    monkeypatch.setattr(gp.biorxiv, "fetch_recent_global", lambda *_a, **_k: [])
    monkeypatch.setattr(gp, "cluster_terms", lambda terms, llm: [
        {"topic": "全部", "species": terms["species"], "terms": terms["others"]}])
    monkeypatch.setattr(gp.time, "sleep", lambda _: None)
    monkeypatch.setattr(gp.top_journals, "fetch_top_journals",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应调用")))
    users = [{"species": ["Apis"], "research_interest": [], "keywords": [], "methods": []}]
    papers = gp.fetch_global_pool(users, FakeLLM(), days=1)
    assert [p.doi for p in papers] == ["10.1/1"]
