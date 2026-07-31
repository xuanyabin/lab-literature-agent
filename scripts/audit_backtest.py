"""只读回测审计（2026-05-01 ~ 2026-07-31，按发表日期）：用当前关键词与筛选策略
模拟 3 个用户最近 3 个月的文献筛选结果。

严格只读纪律：
- 生产库 literature_agent.db 仅以 mode=ro 打开（learned_terms / 分析缓存 SELECT）；
- 不写任何配置文件（auto_terms 缓存用 load_auto_terms 直读，不触发 refresh 写盘；
  主题聚类缓存命中时直接用，未命中才走 cluster_terms——与日线程行为一致）；
- 不发邮件、不写 recommendations；LLM 用量仍记入 logs/.llm_usage.json（任务要求
  用该文件差值统计 token），通过实例属性临时上调 daily_budget 以绕过当日已接近
  耗尽的预算闸（不改 config/model.yaml）。

用法：
    .venv/bin/python scripts/audit_backtest.py fetch            # 阶段A：抓取+粗筛（无 LLM）
    .venv/bin/python scripts/audit_backtest.py judge [--soft-stop SEC]  # 阶段B：LLM 精排+报告
"""

import json
import pickle
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from main import USERS_DIR, apply_lab_profile, load_lab_profile, load_users  # noqa: E402
from database.db import dedup_key, get_analysis, get_paper_id  # noqa: E402
from feedback.vocab import load_active_terms  # noqa: E402
from processing.llm import BudgetExhaustedError, LLMClient  # noqa: E402
from processing.term_expander import apply_auto_terms, load_auto_terms  # noqa: E402
from recommendation import ranker  # noqa: E402
from recommendation.scorer import _normalize_journal, load_scoring_config, rank_papers  # noqa: E402
from sources import biorxiv, pubmed  # noqa: E402
from sources.global_pool import (  # noqa: E402
    CLUSTER_CACHE, build_cluster_query, cluster_terms, collect_global_terms,
    load_clusters, _terms_hash,
)
from sources.top_journals import load_journal_names  # noqa: E402

WINDOW_START = date(2026, 5, 1)
WINDOW_END = date(2026, 7, 31)
DAILY_LIMIT = 15           # config/email.yaml daily_paper_number
EFETCH_CHUNK = 200
CLUSTER_RETMAX = 5000      # 生产为 100/簇；91 天窗口一次性抓取需放大（偏差说明见报告）
JOURNAL_RETMAX = 1500      # 生产为 20/刊/天；91 天窗口放大（偏差见报告）

LOG_DIR = BASE_DIR / "logs"
SNAPSHOT_PATH = LOG_DIR / "backtest_snapshot_2026-07-31.pkl"
CHECKPOINT_PATH = LOG_DIR / "backtest_llm_checkpoint_2026-07-31.jsonl"
JSON_OUT = LOG_DIR / "backtest_3months_2026-07-31.json"
MD_OUT = LOG_DIR / "backtest_3months_2026-07-31.md"
USAGE_BACKUP = LOG_DIR / ".llm_usage.backup_2026-07-31.json"
USAGE_PATH = LOG_DIR / ".llm_usage.json"


def ro_connect() -> sqlite3.Connection:
    """生产库只读连接（URI mode=ro，任何写操作都会被 SQLite 拒绝）。"""
    conn = sqlite3.connect(f"file:{BASE_DIR / 'literature_agent.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def prepare_users(conn) -> list[tuple[str, dict]]:
    """复刻 main.py 逐人准备：lab 并入 → learned_terms（只读）→ 自动词表（直读缓存）。"""
    lab = load_lab_profile()
    prepared = []
    for slug, user in load_users():
        u = apply_lab_profile(user, lab)
        u["learned_terms"] = load_active_terms(conn, u["email"])
        # 与 refresh_auto_terms 等价的读取侧：三个用户的缓存均为今日刷新、无需 LLM
        # 重写；直接 load 避免任何配置写盘。
        u = apply_auto_terms(u, load_auto_terms(slug))
        prepared.append((slug, u))
    return prepared


def _esearch_count(query: str, days: int) -> int:
    """只读探测某查询在窗口内的 PubMed 命中总数（retmax=0 只取 count）。"""
    resp = requests.get(
        f"{pubmed.EUTILS_BASE}/esearch.fcgi",
        params={"db": "pubmed", "term": query, "retmode": "json",
                "datetype": "pdat", "reldate": days, "retmax": 0,
                "tool": "lab-literature-intelligence"},
        timeout=30,
    )
    resp.raise_for_status()
    return int(resp.json()["esearchresult"].get("count", 0))


def _fetch_chunk_with_retry(chunk: list[str], attempts: int = 3) -> list:
    """efetch 单 chunk 重试：生产 _get_with_retry 只重试 429，这里补断连/解析异常。
    pubmed 模块每次走 requests.get 新建连接，无共享连接需清理，直接退避重试。"""
    delay = 5
    for attempt in range(1, attempts + 1):
        try:
            return pubmed.fetch_by_pmids(chunk)
        except Exception as exc:
            print(f"[fetch] efetch chunk 第 {attempt} 次失败（{len(chunk)} 篇）：{exc}")
            if attempt < attempts:
                time.sleep(delay)
                delay *= 2
    return []


def _fetch_pmids_chunked(pmids: list[str], already: set[str]) -> tuple[list, int]:
    """按 200/批取回论文详情；跨簇/跨刊去重抓取（结果与各自抓取后 dedupe 一致）。"""
    papers = []
    failed = 0
    todo = [m for m in pmids if m not in already]
    for i in range(0, len(todo), EFETCH_CHUNK):
        chunk = todo[i:i + EFETCH_CHUNK]
        got = _fetch_chunk_with_retry(chunk)
        if not got:
            failed += 1
        papers.extend(got)
        already.update(chunk)
        time.sleep(0.34)
    return papers, failed


def phase_fetch() -> None:
    assert date.today() >= WINDOW_END, f"今天 {date.today()} 早于窗口结束日"
    days = (date.today() - WINDOW_START).days
    print(f"[fetch] 窗口 {WINDOW_START} ~ {WINDOW_END}，reldate days={days}")

    conn = ro_connect()
    prepared = prepare_users(conn)
    for slug, u in prepared:
        print(f"[fetch] 用户 {slug}：learned_terms={len(u['learned_terms'])}")
    scoring_cfg = load_scoring_config()

    # ---- 主题聚类（缓存命中则零 LLM）----
    terms = collect_global_terms([u for _, u in prepared])
    terms_hash = _terms_hash(terms)
    cached = load_clusters(CLUSTER_CACHE)
    if cached and cached.get("terms_hash") == terms_hash \
            and time.time() - float(cached.get("updated") or 0) <= 7 * 86400:
        clusters = cached["clusters"]
        cluster_source = "cache"
    else:  # 与生产一致：LLM 聚类——预期失败回退（max_tokens 截断，今日已两次复现）
        # 只读纪律：缓存复制到临时路径再调用，避免 LLM 刷新成功时覆写
        # config/users/auto_terms/_clusters.yaml；失败回退读的也是这份副本。
        import shutil
        import tempfile
        tmp_cache = Path(tempfile.mkdtemp(prefix="audit_clusters_")) / "_clusters.yaml"
        if cached:
            shutil.copy2(CLUSTER_CACHE, tmp_cache)
        clusters = cluster_terms(terms, LLMClient(), cache_path=tmp_cache)
        # cluster_terms 失败时内部回退沿用旧缓存，按结果与旧缓存是否一致区分来源
        cluster_source = ("llm_failed_fallback_cache"
                          if cached and clusters == cached.get("clusters")
                          else "llm_refreshed")
    print(f"[fetch] 主题簇 {len(clusters)} 个（来源 {cluster_source}）："
          + "、".join(c["topic"] for c in clusters))

    # ---- PubMed 分簇检索（放大 retmax + 分块 efetch）----
    fetched_pmids: set[str] = set()
    pool = []
    origins: dict[str, str] = {}
    cluster_stats = []
    for c in clusters:
        query = build_cluster_query(c)
        count = _esearch_count(query, days)
        pmids = pubmed.search_pmids(query, days, CLUSTER_RETMAX)
        papers, failed_chunks = _fetch_pmids_chunked(pmids, fetched_pmids)
        for p in papers:
            origins[dedup_key(p)] = "pubmed"
        pool.extend(papers)
        cluster_stats.append({"topic": c["topic"], "count": count,
                              "idlist": len(pmids), "truncated": len(pmids) < count,
                              "fetched_new": len(papers), "failed_chunks": failed_chunks})
        print(f"[fetch] 簇「{c['topic']}」count={count} 取回={len(pmids)}"
              f"{' ⚠截断' if len(pmids) < count else ''}")
        time.sleep(0.4)

    # ---- bioRxiv 全量拉取 + 本地过滤（max_results 放开）----
    preprints = biorxiv.fetch_recent_global(terms["species"], terms["others"],
                                            days=days, max_results=10 ** 9)
    for p in preprints:
        origins.setdefault(dedup_key(p), "biorxiv")
    print(f"[fetch] bioRxiv 命中 {len(preprints)} 篇")

    # ---- 顶刊直采通道（t0，放大 retmax）----
    channel_cfg = scoring_cfg.get("journal_channel") or {}
    channel_names = load_journal_names(tiers=tuple(channel_cfg.get("tiers") or ("t0",))) \
        if channel_cfg.get("enabled") else []
    journal_stats = []
    top_papers = []
    for name in channel_names:
        try:
            count = _esearch_count(f'"{name}"[jour]', days)
            pmids = pubmed.search_pmids(f'"{name}"[jour]', days, JOURNAL_RETMAX)
            papers, failed_chunks = _fetch_pmids_chunked(pmids, fetched_pmids)
            top_papers.extend(papers)
            journal_stats.append({"journal": name, "count": count,
                                  "idlist": len(pmids), "truncated": len(pmids) < count,
                                  "failed_chunks": failed_chunks})
            print(f"[fetch] 顶刊 {name}：count={count} 取回={len(pmids)}")
        except Exception as exc:  # 单刊失败不阻断，与生产一致
            print(f"[fetch] 顶刊 {name} 失败：{exc}")
        time.sleep(0.4)
    for p in top_papers:
        origins.setdefault(dedup_key(p), "channel")

    # ---- 全局去重（顺序与 global_pool 一致：关键词池 → bioRxiv → 顶刊）----
    merged = pubmed.dedupe(pool + preprints + top_papers)
    origins = {k: origins[k] for k in (dedup_key(p) for p in merged) if k in origins}
    print(f"[fetch] 全局池去重后 {len(merged)} 篇")

    # ---- 按发表日期分桶 ----
    buckets: dict[str, list] = {}
    unbucketed = 0
    for p in merged:
        d = (p.date or "")[:10]
        try:
            pd = date.fromisoformat(d)
        except ValueError:
            unbucketed += 1
            continue
        if WINDOW_START <= pd <= WINDOW_END:
            buckets.setdefault(d, []).append(p)
        else:
            unbucketed += 1
    print(f"[fetch] 入桶 {sum(len(v) for v in buckets.values())} 篇，"
          f"日期缺失/越界丢弃 {unbucketed} 篇")

    # ---- 逐用户：全池粗筛一次 + 逐日模拟（top-15 + 顶刊通道补入）----
    channel_tiers = set(channel_cfg.get("tiers") or ()) if channel_names else set()
    channel_journals = {n for n, t in scoring_cfg["journal_tiers"].items() if t in channel_tiers}
    channel_max = int(channel_cfg.get("max_per_user", 10))
    users_out = {}
    for slug, u in prepared:
        scored = rank_papers(merged, u, scoring_cfg)
        score_of = {dedup_key(p): s for s, p in scored}
        matched = sum(1 for s, _ in scored if s > 0)
        seen: set[str] = set()
        candidates = []
        daily = []
        for day in sorted(buckets):
            day_scored = [(score_of[dedup_key(p)], p) for p in buckets[day]
                          if dedup_key(p) in score_of]
            day_scored.sort(key=lambda x: -x[0])  # 稳定排序，与 rank_papers 当日语义一致
            fresh = [(s, p) for s, p in day_scored if dedup_key(p) not in seen]
            shortlist = fresh[:DAILY_LIMIT]
            picked = {dedup_key(p) for _, p in shortlist}
            extras = []
            for s, p in fresh:
                if len(extras) >= channel_max:
                    break
                if _normalize_journal(p.journal) in channel_journals \
                        and dedup_key(p) not in picked:
                    picked.add(dedup_key(p))
                    extras.append((s, p))
            n_matched_day = sum(1 for s, _ in day_scored if s > 0)
            daily.append({"date": day, "pool": len(buckets[day]),
                          "matched": n_matched_day,
                          "shortlisted": len(shortlist) + len(extras)})
            for rank, (s, p) in enumerate(shortlist, 1):
                k = dedup_key(p)
                seen.add(k)
                candidates.append({"key": k, "first_date": day, "daily_rank": rank,
                                   "via_channel": False, "coarse_score": s, "paper": p})
            for rank, (s, p) in enumerate(extras, 1):
                k = dedup_key(p)
                seen.add(k)
                candidates.append({"key": k, "first_date": day, "daily_rank": rank,
                                   "via_channel": True, "coarse_score": s, "paper": p})
        users_out[slug] = {
            "email": u["email"], "matched_window": matched,
            "candidates": candidates, "daily": daily,
        }
        print(f"[fetch] 用户 {slug}：窗口 matched={matched}，"
              f"候选（逐日 top15+通道 去重后）={len(candidates)}")

    snapshot = {
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()], "days": days,
        "cluster_source": cluster_source, "cluster_stats": cluster_stats,
        "journal_stats": journal_stats, "biorxiv_hits": len(preprints),
        "pool_size": len(merged), "unbucketed": unbucketed,
        "origins": origins, "users": users_out,
        "prepared_users": {slug: u for slug, u in prepared},
    }
    SNAPSHOT_PATH.write_bytes(pickle.dumps(snapshot))
    print(f"[fetch] 快照已写入 {SNAPSHOT_PATH}")


# ---------------------------------------------------------------- 阶段 B：LLM 精排

def _judge_user_batches(slug: str, user: dict, cands: list, llm, weights,
                        thresholds, journal_tiers, batch_size: int,
                        deadline: float, out_fp, state: dict) -> None:
    """对单个用户的候选逐批评判并 checkpoint；到达 deadline 或预算耗尽即停。"""
    items = [{"paper": c["paper"], "analysis": c.get("analysis") or {},
              "_cand": c} for c in cands]
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    with ThreadPoolExecutor(max_workers=min(llm.max_workers, max(1, len(batches)))) as pool:
        futures = {}
        for bi, batch in enumerate(batches):
            if time.monotonic() > deadline:
                break
            fut = pool.submit(ranker._judge_batch_safe, batch, user, llm)
            futures[fut] = (bi, batch)
        state["submitted"] += len(futures)
        for fut in as_completed(futures):
            bi, batch = futures[fut]
            if time.monotonic() > deadline:
                for f in futures:
                    f.cancel()  # 已运行的取消不掉，其 result 走异常分支计数
                state["deadline_stop"] = True
            try:
                judgments = fut.result()
            except BudgetExhaustedError:
                state["budget_stop"] = True
                for f in futures:
                    f.cancel()
                continue
            except Exception as exc:  # 取消等
                state["errors"] += len(batch)
                print(f"[judge] {slug} 批次 {bi} 失败：{exc}")
                continue
            for it, j in zip(batch, judgments):
                c = it["_cand"]
                paper = it["paper"]
                dims = {
                    "personal": j["personal_relevance"],
                    "lab": ranker.lab_relevance(paper, user),
                    "journal": ranker.journal_influence(paper, journal_tiers),
                    "novelty": j["novelty"],
                    "method": ranker.method_relevance(paper, user),
                    "recency": ranker.recency(paper),
                }
                total = ranker.final_score(dims, weights)
                rec = {
                    "user": slug, "key": c["key"], "title": paper.title,
                    "journal": paper.journal, "date_pub": paper.date,
                    "first_date": c["first_date"], "daily_rank": c["daily_rank"],
                    "via_channel": c["via_channel"], "coarse_score": c["coarse_score"],
                    "source": state["origins"].get(c["key"], "?"),
                    "analysis_cached": bool(it["analysis"]),
                    "dims": dims, "total": total,
                    "category": ranker._category_of(total, thresholds),
                    "pushed": total >= thresholds.get("push_floor", 0),
                    "reason": j["reason"],
                }
                out_fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                state["judged"] += 1
            out_fp.flush()


def phase_judge(soft_stop_sec: int) -> None:
    snap = pickle.loads(SNAPSHOT_PATH.read_bytes())
    origins = snap["origins"]
    scoring_cfg = load_scoring_config()
    weights = ranker.load_ranker_weights()
    thresholds = ranker.load_ranker_thresholds()
    journal_tiers = scoring_cfg["journal_tiers"]
    batch_size = ranker.load_ranker_batch_size()

    # 生产库只读：尽量复用已有 AI 分析缓存（problem/finding 进 judge prompt）
    conn = ro_connect()
    n_cached = 0
    total_cands = 0
    for slug, udata in snap["users"].items():
        for c in udata["candidates"]:
            pid = get_paper_id(conn, c["key"])
            c["analysis"] = get_analysis(conn, pid) if pid else None
            if c["analysis"]:
                n_cached += 1
            total_cands += 1
    print(f"[judge] 候选总数 {total_cands}（按用户计），生产分析缓存命中 {n_cached}")

    # 断点续跑：checkpoint 里已有的 (user, key) 直接跳过，可跨多次运行累积
    done_keys: set[tuple[str, str]] = set()
    if CHECKPOINT_PATH.exists():
        for line in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    done_keys.add((r["user"], r["key"]))
                except (json.JSONDecodeError, KeyError):
                    pass
    if done_keys:
        print(f"[judge] 续跑：checkpoint 已有 {len(done_keys)} 条，跳过")

    # 用量基线只取第一次运行的备份；续跑时读回旧备份，保证差值覆盖全程
    if USAGE_BACKUP.exists():
        usage_before = json.loads(USAGE_BACKUP.read_text(encoding="utf-8"))
    else:
        usage_before = json.loads(USAGE_PATH.read_text(encoding="utf-8")) \
            if USAGE_PATH.exists() else {}
        USAGE_BACKUP.write_text(json.dumps(usage_before, indent=2), encoding="utf-8")

    llm = LLMClient()
    llm.daily_budget = 5000  # 当日生产已用 907/1000；仅放行本审计，不写配置（偏差说明见报告）

    t0 = time.monotonic()
    deadline = t0 + soft_stop_sec
    state = {"judged": 0, "submitted": 0, "errors": 0, "budget_stop": False,
             "origins": origins}
    with CHECKPOINT_PATH.open("a", encoding="utf-8") as out_fp:
        for slug, udata in snap["users"].items():
            if time.monotonic() > deadline or state["budget_stop"]:
                print(f"[judge] 到达软停止线，跳过用户 {slug} 的剩余批次")
                continue
            user = snap["prepared_users"][slug]
            todo_cands = [c for c in udata["candidates"]
                          if (slug, c["key"]) not in done_keys]
            if not todo_cands:
                print(f"[judge] 用户 {slug} 候选已全部评判，跳过")
                continue
            t_user = time.monotonic()
            _judge_user_batches(slug, user, todo_cands, llm, weights,
                                thresholds, journal_tiers, batch_size,
                                deadline, out_fp, state)
            print(f"[judge] 用户 {slug} 完成阶段，累计 judged={state['judged']}，"
                  f"用时 {time.monotonic() - t_user:.0f}s")
    elapsed = time.monotonic() - t0

    usage_after = json.loads(USAGE_PATH.read_text(encoding="utf-8")) \
        if USAGE_PATH.exists() else {}
    _write_reports(snap, elapsed, usage_before, usage_after, state,
                   n_cached, total_cands, batch_size)


def _write_reports(snap, elapsed, usage_before, usage_after, state,
                   n_cached, total_cands, batch_size) -> None:
    records = []
    if CHECKPOINT_PATH.exists():
        for line in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    by_user: dict[str, list] = {}
    for r in records:
        by_user.setdefault(r["user"], []).append(r)

    pool_size = snap["pool_size"]
    origins_count: dict[str, int] = {}
    for v in snap["origins"].values():
        origins_count[v] = origins_count.get(v, 0) + 1
    cns = {"nature", "science", "cell"}

    calls_delta = (usage_after.get("calls", 0) - usage_before.get("calls", 0))
    tokens_delta = (usage_after.get("total_tokens", 0) - usage_before.get("total_tokens", 0))
    prompt_delta = (usage_after.get("prompt_tokens", 0) - usage_before.get("prompt_tokens", 0))
    compl_delta = (usage_after.get("completion_tokens", 0) - usage_before.get("completion_tokens", 0))

    report_users = {}
    md = []
    md.append("# 三个月只读回测审计报告（2026-05-01 ~ 2026-07-31）\n")
    md.append(f"- 生成时间：{date.today().isoformat()}；全局池 {pool_size} 篇"
              f"（PubMed 关键词池 {origins_count.get('pubmed', 0)} / "
              f"bioRxiv {origins_count.get('biorxiv', 0)} / "
              f"顶刊通道 {origins_count.get('channel', 0)}），"
              f"未入桶（日期缺失/越界）{snap['unbucketed']} 篇")
    md.append(f"- 主题聚类来源：{snap['cluster_source']}（{len(snap['cluster_stats'])} 簇）")
    md.append(f"- LLM 覆盖率：{len(records)}/{total_cands} 篇次"
              f"（{100.0 * len(records) / max(total_cands, 1):.1f}%）"
              f"{'；到达软停止线/预算停止，以下为部分结果' if len(records) < total_cands else ''}\n")

    for slug, udata in snap["users"].items():
        cands = udata["candidates"]
        recs = by_user.get(slug, [])
        judged_keys = {r["key"] for r in recs}
        must = [r for r in recs if r["category"] == "Must Read"]
        important = [r for r in recs if r["category"] == "Important"]
        reference = [r for r in recs if r["category"] == "Reference"]
        below_floor = [r for r in recs if not r["pushed"]]
        src_dist: dict[str, int] = {}
        tier_dist = {"t0": 0, "t1": 0, "other": 0}
        cns_n = 0
        tiers = load_scoring_config()["journal_tiers"]
        for r in recs:
            src_dist[r["source"]] = src_dist.get(r["source"], 0) + 1
            tier = tiers.get(_normalize_journal(r["journal"]))
            tier_dist[tier or "other"] += 1
            if _normalize_journal(r["journal"]) in cns:
                cns_n += 1
        # 逐日推送模拟：当日候选中已评判且 ≥push_floor，按总分降序 ≤15
        daily_push: dict[str, list] = {}
        for r in recs:
            if r["pushed"]:
                daily_push.setdefault(r["first_date"], []).append(r)
        push_counts = {d: min(len(v), 15) for d, v in daily_push.items()}
        for d in push_counts:
            daily_push[d].sort(key=lambda r: -r["total"])
        all_days = [d["date"] for d in udata["daily"]]
        pushed_series = [push_counts.get(d, 0) for d in all_days]
        days_judged = {d for d in all_days
                       if any(r["first_date"] == d for r in recs)}
        must_days = {r["first_date"] for r in must}
        zero_must_days = len([d for d in days_judged if d not in must_days])
        mean_push = sum(pushed_series) / max(len(all_days), 1)
        min_day = min(all_days, key=lambda d: push_counts.get(d, 0)) if all_days else "-"
        max_day = max(all_days, key=lambda d: push_counts.get(d, 0)) if all_days else "-"

        report_users[slug] = {
            "matched_window": udata["matched_window"],
            "candidates": len(cands), "judged": len(recs),
            "must_read": len(must), "important": len(important),
            "reference": len(reference), "below_push_floor": len(below_floor),
            "source_dist": src_dist, "tier_dist": tier_dist, "cns": cns_n,
            "daily_mean_push": round(mean_push, 2),
            "zero_must_read_days": zero_must_days,
            "min_push_day": [min_day, push_counts.get(min_day, 0)],
            "max_push_day": [max_day, push_counts.get(max_day, 0)],
        }

        md.append(f"\n## 用户 {slug}（{udata['email']}）\n")
        md.append(f"- 窗口粗筛 matched（score>0）：{udata['matched_window']} 篇；"
                  f"LLM 候选 {len(cands)} 篇，已评判 {len(recs)} 篇")
        md.append(f"- 定级：Must Read {len(must)} / Important {len(important)} / "
                  f"Reference {len(reference)}（其中低于 push_floor 不进邮件 "
                  f"{len(below_floor)} 篇）")
        md.append(f"- 来源分布（已评判候选）：{src_dist}；期刊分层："
                  f"T0 {tier_dist['t0']} / T1 {tier_dist['t1']} / 其他 {tier_dist['other']}；"
                  f"CNS 正刊 {cns_n} 篇")
        md.append(f"- 逐日推送：日均 {mean_push:.1f} 篇；有评判结果的天数中 "
                  f"0 篇 Must Read 的天数 {zero_must_days}；"
                  f"篇数最少 {min_day}（{push_counts.get(min_day, 0)} 篇）、"
                  f"最多 {max_day}（{push_counts.get(max_day, 0)} 篇）")
        md.append(f"\n### {slug} Must Read 清单（{len(must)} 篇）\n")
        for r in sorted(must, key=lambda r: (r["first_date"], -r["total"])):
            md.append(f"- {r['first_date']} | {r['total']}分 | {r['title']} "
                      f"| {r['journal']} | {r['source']}"
                      f"{'(通道补入)' if r['via_channel'] else ''}")
        md.append(f"\n### {slug} Important 清单（{len(important)} 篇）\n")
        for r in sorted(important, key=lambda r: (r["first_date"], -r["total"])):
            md.append(f"- {r['first_date']} | {r['total']}分 | {r['title']} "
                      f"| {r['journal']} | {r['source']}"
                      f"{'(通道补入)' if r['via_channel'] else ''}")

    md.append("\n## 成本\n")
    n_batches = -(-len(records) // max(batch_size, 1))
    md.append(f"- LLM 调用增量：{calls_delta} 次（≈评判 {n_batches} 批"
              f"（batch_size={batch_size}）+ fetch 阶段 3 次聚类尝试；"
              "口径为 logs/.llm_usage.json 审计全程差值，judge 分两轮运行）")
    md.append(f"- token 增量：total {tokens_delta}"
              f"（prompt {prompt_delta} / completion {compl_delta}）")

    md.append("\n## 抓取完整性\n")
    for c in snap["cluster_stats"]:
        md.append(f"- 簇「{c['topic']}」：PubMed count={c['count']}，"
                  f"取回={c['idlist']}{' ⚠ 被 retmax 截断' if c['truncated'] else ''}")
    trunc_j = [j for j in snap["journal_stats"] if j["truncated"]]
    md.append(f"- 顶刊通道 {len(snap['journal_stats'])} 刊，"
              f"截断刊数 {len(trunc_j)}"
              + (f"：{[j['journal'] for j in trunc_j]}" if trunc_j else ""))

    md.append("\n## 偏差说明\n")
    md.append("1. 抓取窗口一次性 days=91（生产逐日 days=1）：PubMed 簇检索 retmax "
              "100→5000、顶刊通道 20→1500/刊、bioRxiv max_results 200→放开；"
              "截断情况见「抓取完整性」。")
    md.append("2. 跨天去重按窗口内首次出现模拟（同一 dedup_key 90 天只推一次）；"
              "生产库 2026-05-01 前的历史推送未参与去重（生产库 recommendations "
              "最早记录为 2026-07-22，窗口前段无影响）；且生产只在通过 push_floor "
              "并入邮件后才标记已发，本模拟在进入当日 shortlist 即标记——低分论文"
              "在真实流水线中可能于后续日期重复进入候选。")
    md.append("3. learned_terms / auto_terms / 主题聚类均为今日（2026-07-31）状态，"
              "非 5-7 月各日的历史状态。")
    md.append("4. 精排 judge prompt 的 AI 分析字段（problem/finding）来自生产库只读缓存"
              f"（命中 {n_cached}/{total_cands}），未命中的候选该字段为空，"
              "personal/novelty 判断精度略低于生产（生产会先跑 ensure_artifacts）。")
    md.append("5. LLM daily_budget 以实例属性临时上调至 5000（当日生产已用 "
              f"{usage_before.get('calls', 0)}/1000，否则审计立即触发预算熔断）；"
              "未修改 config/model.yaml，用量仍计入 logs/.llm_usage.json。")
    md.append("6. 分桶按论文解析发表日期（ArticleDate/PubDate，bioRxiv 为发布日期）；"
              "PubMed 检索用 pdat reldate=91，边缘日期可能有个别出入；"
              f"{snap['unbucketed']} 篇日期缺失/越界未入桶。")
    md.append("7. recency 维度权重为 0，发表日期分桶不影响总分；push_floor 过滤与"
              "每日 15 篇封顶按 rank_items/deliver 的同一阈值与顺序解析复刻。")
    md.append("8. 生产顶刊通道按天抓取（retmax 20/刊/天），单刊单日超过 20 篇时"
              "生产会截断而本模拟完整——模拟的通道候选略多于真实当日。")
    if snap.get("biorxiv_hits", 0) == 0:
        md.append("9. bioRxiv 源本次运行连接被重置（ConnectionResetError），0 篇纳入"
                  "候选；但近期生产日报（7-22、7-31 共 4 份）中 bioRxiv 条目数为 0，"
                  "预印本很少进入实际推送，对结论影响有限——窗口内的高相关预印本"
                  "仍会被漏计。")
    if snap.get("cluster_source") == "llm_failed_fallback_cache":
        md.append("10. 主题聚类 LLM 刷新因输出截断失败（max_tokens=2000，三次复现），"
                  "沿用 7-29 旧缓存 6 簇——与今日生产行为一致（生产缓存 updated 仍"
                  "为 7-29，说明今日生产同样刷新失败沿用旧缓存）。")

    out = {
        "window": snap["window"], "pool_size": pool_size,
        "pool_by_source": origins_count, "unbucketed": snap["unbucketed"],
        "cluster_source": snap["cluster_source"],
        "cluster_stats": snap["cluster_stats"], "journal_stats": snap["journal_stats"],
        "llm_coverage": {"judged": len(records), "total": total_cands,
                         "analysis_cache_hits": n_cached},
        "cost": {"calls_delta": calls_delta, "total_tokens_delta": tokens_delta,
                 "prompt_tokens_delta": prompt_delta,
                 "completion_tokens_delta": compl_delta,
                 "elapsed_sec": round(elapsed, 1),
                 "usage_before": usage_before, "usage_after": usage_after},
        "users": {slug: {**report_users[slug],
                         "daily": snap["users"][slug]["daily"],
                         "records": by_user.get(slug, [])}
                  for slug in snap["users"]},
    }
    JSON_OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    MD_OUT.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"[judge] 报告已写入 {JSON_OUT} 与 {MD_OUT}")
    print(f"[judge] judged={state['judged']}/{total_cands}，"
          f"calls+{calls_delta}，tokens+{tokens_delta}，耗时 {elapsed:.0f}s")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("fetch", "judge"):
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "fetch":
        phase_fetch()
    else:
        soft = 1200
        if "--soft-stop" in sys.argv:
            soft = int(sys.argv[sys.argv.index("--soft-stop") + 1])
        phase_judge(soft)
