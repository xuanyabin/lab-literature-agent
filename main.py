"""每日文献情报流水线入口（Phase 6：全局池 + 产物复用）。

流程：遍历 config/users/ 下所有 active 用户，先做逐人词表准备——加载反馈学习词表
      （Phase 5，与手配词表分离，参与检索与粗筛）→ 加载自动词表
      （config/users/auto_terms/<slug>.yaml：LLM 扩展词仅用于召回、等权，
      缓存缺失/用户 yaml 更新/超 7 天时自动刷新，失败沿用旧缓存）；
      然后全局合并检索一次（sources/global_pool.py：全用户词表合并 → LLM 主题
      聚类分簇检索 PubMed + bioRxiv 全局过滤，可选并入顶刊直采通道
      sources/top_journals.py——按 journals.yaml 刊名直抓绕过关键词召回，
      拼成当日全局池）→ 每用户本地规则
      粗筛等权打分选出候选（实验室公共方向词叠加个人词表；期刊因素只在精排
      journal 维度体现，粗筛不再按期刊加分；顶刊通道论文按刊名补入候选，
      每用户每日上限见 scoring.yaml 的 journal_channel）→ 按用户跨天去重 → top-N；
      各用户 shortlist 求并集，并集只做一次 LLM 处理（分析/新闻摘要/翻译，
      SQLite 全局表缓存复用，同一篇论文全实验室只处理一次；未命中缓存的论文按
      model.yaml max_workers 并发调 LLM）→ 每用户个性化精排
      （六维加权 Final Score + AI 推荐理由；LLM 判断按 scoring.yaml ranker.batch_size
      分批一次调用评判多篇、批次间并发；按 Final Score 绝对阈值定级
      Must Read / Important / Reference，低于 push_floor 推送下限的不进邮件，
      超过 --limit 时按 Final Score 截断封顶，宁缺毋滥）→ 每日价值总结 → HTML 邮件
      （卡片内嵌 ⭐1-5 mailto 反馈链接 + Part 3 批量标注回信入口，
      均由 python -m feedback 经 IMAP 收集学习）；
      论文与产物入库 SQLite，推荐记录写入 recommendations 表
      （用户之间去重互不影响）。LLM 日预算耗尽时快速失败，不发空壳邮件。

用法：
    python main.py                      # 对所有 active 用户执行完整流程并发送邮件
    python main.py --dry-run            # 不发邮件，HTML 写入 logs/
    python main.py --user user001       # 只对指定用户执行（调试用；全局池也只按入选用户词表构建）
    python main.py --days 3 --limit 15  # 回溯 3 天，最多 15 篇（默认篇数见 config/email.yaml）
    python -m feedback                  # 收集反馈回信并执行学习闭环（建议每日先跑）
"""

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

from database.db import connect, dedup_key, get_seen_keys, save_recommendation
from feedback.vocab import load_active_terms
from mailer.digest_builder import build_digest_html, load_email_config
from mailer.sender import send_email
from processing.artifacts import ensure_artifacts
from processing.daily_summary_generator import generate_daily_summary
from processing.llm import BudgetExhaustedError, LLMClient
from processing.term_expander import apply_auto_terms, refresh_auto_terms
from recommendation.ranker import (
    load_ranker_batch_size, load_ranker_thresholds, load_ranker_weights, rank_items,
)
from recommendation.scorer import _normalize_journal, load_scoring_config, rank_papers
from sources.global_pool import fetch_global_pool
from sources.top_journals import load_journal_names

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
USERS_DIR = BASE_DIR / "config" / "users"
LAB_CONFIG = BASE_DIR / "config" / "lab.yaml"


def load_user(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_users(users_dir: Path = USERS_DIR) -> list[tuple[str, dict]]:
    """读取 users_dir 下所有 active 用户，返回 [(slug, user)]（按文件名排序）。"""
    users = []
    for path in sorted(users_dir.glob("*.yaml")):
        user = load_user(path)
        if user.get("active", True):
            users.append((path.stem, user))
    return users


def load_lab_profile(path: Path = LAB_CONFIG) -> dict:
    """读取实验室公共方向配置（config/lab.yaml），文件缺失时返回空配置。"""
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def apply_lab_profile(user: dict, lab: dict) -> dict:
    """把实验室公共方向并入用户配置副本（V5 全分组版）：

    - lab_recall = default_groups（全员自动订阅组）+ 用户订阅的 topic_groups 展开
      （进全局召回并参与粗筛打分，按词去重）；
    - lab_topics = lab_recall + rank_only（精排 lab 维度接口；rank_only 不进检索式、不打粗筛分）；
    - noise_terms = 医学噪音词（粗筛软惩罚，减分不淘汰）；
    - 别名表合并（个人优先）。旧版 global_core/topics 平铺键按一组额外词处理（向后兼容）。
    """
    merged = dict(user)
    recall = list(lab.get("global_core") or lab.get("topics") or [])
    groups = lab.get("topic_groups") or {}
    subscribed = list(lab.get("default_groups") or []) + list(user.get("topic_groups") or [])
    for name in subscribed:
        if name in groups:
            recall += list(groups[name] or [])
        else:
            logging.getLogger(__name__).warning("订阅了不存在的 topic_group：%s，忽略", name)
    seen: set[str] = set()
    recall = [t for t in recall
              if (k := (t or "").strip().lower()) and not (k in seen or seen.add(k))]
    lab_topics = list(recall)
    for t in lab.get("rank_only") or []:
        k = (t or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            lab_topics.append(t)
    merged["lab_recall"] = recall
    merged["lab_topics"] = lab_topics
    merged["noise_terms"] = list(lab.get("noise_terms") or [])
    merged["aliases"] = {**(lab.get("aliases") or {}), **(user.get("aliases") or {})}
    return merged


def _personal_title_fallback(fresh: list, shortlist: list, user: dict, max_extra: int) -> list:
    """个人关键词强命中兜底（V5）：未进 top-N 截断的候选中，任意个人词
    （species/keywords/research_interest/methods，aliases 展开）命中标题者追加
    （最多 max_extra 篇），兜底个人强相关论文被 lab 词高分挤掉的情况。"""
    if max_extra <= 0:
        return []
    picked = {dedup_key(p) for _, p in shortlist}
    aliases = user.get("aliases") or {}
    variants: list[str] = []
    for field in ("species", "keywords", "research_interest", "methods"):
        for term in user.get(field) or []:
            if not term or not term.strip():
                continue
            term = term.strip()
            variants += [v.strip().lower() for v in [term, *(aliases.get(term) or [])] if v and v.strip()]
    extras = []
    for s, p in fresh:
        if len(extras) >= max_extra:
            break
        if dedup_key(p) in picked:
            continue
        if any(v in p.title.lower() for v in variants):
            picked.add(dedup_key(p))
            extras.append((s, p))
    return extras


def deliver(slug: str, user: dict, shortlist: list, artifacts: dict,
            args: argparse.Namespace, email_cfg: dict, scoring_cfg: dict,
            llm: LLMClient, conn, log: logging.Logger,
            pool_total: int = 0, matched: int = 0) -> None:
    """单用户投递：从全局产物取本用户 shortlist → 个性化精排定级 → 价值总结 → 生成并投递邮件。

    pool_total 为当日全局池总新文献数，matched 为该用户粗筛 score>0 的篇数
    （已见去重之前），两者汇入邮件开头总览块。
    """
    if not shortlist:
        log.info("用户 %s 今日无新论文", slug)
        return
    log.info("用户：%s <%s>，进入精排 %d 篇", user["name"], user["email"], len(shortlist))

    items = []
    for score, paper in shortlist:
        a = artifacts[dedup_key(paper)]
        items.append({
            "paper": paper,
            "analysis": a["analysis"],
            "news": a["news"],
            "title_zh": a["title_zh"],
            "background": a["background"],
            "methods": a["methods"],
            "results": a["results"],
            "significance": a["significance"],
        })

    log.info("个性化精排：六维加权 Final Score + 生成推荐理由（批量 %d 篇/次）",
             load_ranker_batch_size())
    items = rank_items(items, user, llm, scoring_cfg["journal_tiers"],
                       load_ranker_weights(), load_ranker_thresholds(),
                       batch_size=load_ranker_batch_size(),
                       max_workers=llm.max_workers)
    if not items:  # 推送下限过滤后为空：宁缺毋滥，今日不发
        log.info("用户 %s 无达到推送下限的论文，跳过今日邮件", slug)
        return
    if len(items) > args.limit:  # 合并封顶：关键词通道与顶刊通道凭 Final Score 竞争
        log.info("超过每日上限 %d 篇，按 Final Score 截断（%d → %d）",
                 args.limit, len(items), args.limit)
        items = items[:args.limit]
    n_must = sum(1 for it in items if it["category"] == "Must Read")
    n_important = sum(1 for it in items if it["category"] == "Important")
    log.info("精排定级：Must Read %d / Important %d / Reference %d",
             n_must, n_important, len(items) - n_must - n_important)
    overview = {
        "days": args.days,
        "pool_total": pool_total,
        "matched": matched,
        "pushed": len(items),
        "must_read": n_must,
        "important": n_important,
        "reference": len(items) - n_must - n_important,
    }

    today = date.today().isoformat()
    if not args.dry_run:  # dry-run 不写库，避免把未发送的论文标记为已发
        for it in items:
            it["paper_id"] = artifacts[dedup_key(it["paper"])]["paper_id"]
            save_recommendation(conn, user["email"], it["paper_id"], it["category"], it["score"], today)

    log.info("生成今日价值总结")
    try:
        daily_summary = generate_daily_summary(items, llm)
    except BudgetExhaustedError:
        raise  # 预算耗尽快速失败：不发空壳邮件
    except Exception:
        # 总结是锦上添花，失败不应阻断发信（模板对空总结有占位文案）
        log.warning("今日价值总结生成失败，邮件照常发送", exc_info=True)
        daily_summary = ""

    html = build_digest_html(user["name"], today, items, daily_summary, email_cfg,
                             user_email=user["email"], overview=overview)

    if args.dry_run:
        out = LOG_DIR / f"digest_{today}_{slug}.html"
        out.write_text(html, encoding="utf-8")
        log.info("dry-run：邮件 HTML 已写入 %s", out)
    else:
        send_email(user["email"], f"Daily Literature Intelligence Report · {today}", html)
        log.info("邮件已发送至 %s", user["email"])


def main() -> int:
    email_cfg = load_email_config()
    load_dotenv()
    # 反馈收件箱（卡片 ⭐1-5 mailto 降级与 Part 3 批量回信都发往这里，由 IMAP 收集）
    email_cfg["feedback_email"] = os.environ.get("DIGEST_FROM_EMAIL", "")
    # 星标一键反馈 webhook（Cloudflare Worker 校验签名后直写 feedback_data/pending/；
    # 两者都配置后 ⭐1-5 点击即完成反馈，任一缺省则降级 mailto 回信）
    email_cfg["feedback_webhook_url"] = os.environ.get("FEEDBACK_WEBHOOK_URL", "")
    email_cfg["feedback_secret"] = os.environ.get("FEEDBACK_SECRET", "")

    parser = argparse.ArgumentParser(description="每日文献情报流水线")
    parser.add_argument("--user", default=None,
                        help="只执行指定用户（config/users/ 下的文件名，不含 .yaml）；默认执行所有 active 用户")
    parser.add_argument("--days", type=int, default=1, help="回溯天数（默认 1）")
    parser.add_argument("--limit", type=int, default=email_cfg["daily_paper_number"],
                        help="最多推荐篇数（默认取 config/email.yaml 的 daily_paper_number）")
    parser.add_argument("--dry-run", action="store_true", help="不发送邮件，HTML 写入 logs/")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    log = logging.getLogger("main")

    if args.user:
        users = [(args.user, load_user(USERS_DIR / f"{args.user}.yaml"))]
    else:
        users = load_users()
    if not users:
        log.info("没有 active 用户，流程结束")
        return 0
    log.info("本次执行 %d 个用户：%s", len(users), ", ".join(slug for slug, _ in users))

    lab = load_lab_profile()
    conn = connect()
    llm = LLMClient()  # 全局唯一实例：日预算跨用户统一计数

    # 逐人词表准备：实验室公共方向 + 反馈学习词表 + 自动词表（扩展词/反馈新增词）
    prepared = []
    for slug, user in users:
        u = apply_lab_profile(user, lab)
        u["learned_terms"] = load_active_terms(conn, u["email"])
        if u["learned_terms"]:
            log.info("用户 %s 学习词表：%d 个有效词参与检索与打分", slug, len(u["learned_terms"]))
        auto = refresh_auto_terms(slug, u, USERS_DIR / f"{slug}.yaml", llm)
        if auto["expansion"] or auto["feedback_added"]:
            u = apply_auto_terms(u, auto)
            log.info("用户 %s 自动词表：%d 个原词的扩展词、%d 个反馈新增关键词参与检索与打分",
                     slug, len(auto["expansion"]), len(auto["feedback_added"]))
        prepared.append((slug, u))

    # 全局合并检索一次：全用户词表聚类分簇 → PubMed + bioRxiv 全局池（可选并入顶刊直采）
    scoring_cfg = load_scoring_config()
    channel_cfg = scoring_cfg.get("journal_channel") or {}
    channel_names = load_journal_names(tiers=tuple(channel_cfg.get("tiers") or ("t0",))) \
        if channel_cfg.get("enabled") else []
    pool = fetch_global_pool(
        [u for _, u in prepared], llm, days=args.days,
        journal_channel={"names": channel_names,
                         "retmax_per_journal": channel_cfg.get("retmax_per_journal", 20)})
    log.info("全局池：%d 篇（去重后）", len(pool))
    if not pool:
        log.info("全局池为空，今日无新文献")
        conn.close()
        return 0

    # 每用户本地粗筛 + 跨天去重（语义与逐人检索时代完全一致，只是池子共享）
    channel_tiers = set(channel_cfg.get("tiers") or ()) if channel_names else set()
    channel_journals = {name for name, tier in scoring_cfg["journal_tiers"].items()
                        if tier in channel_tiers}
    channel_max = int(channel_cfg.get("max_per_user", 10))
    shortlists: dict[str, list] = {}
    matched_counts: dict[str, int] = {}
    for slug, u in prepared:
        scored = rank_papers(pool, u, scoring_cfg)
        matched_counts[slug] = sum(1 for s, _ in scored if s > 0)
        if scored:
            log.info("用户 %s 规则粗筛：候选分数区间 %d–%d", slug, scored[-1][0], scored[0][0])
        seen = get_seen_keys(conn, u["email"])
        fresh = [(s, p) for s, p in scored if dedup_key(p) not in seen]
        if len(fresh) < len(scored):
            log.info("用户 %s 跨天去重：%d 篇已在历史邮件中出现过，跳过",
                     slug, len(scored) - len(fresh))
        shortlist = fresh[:args.limit]
        fallback_cfg = scoring_cfg.get("personal_fallback") or {}
        if fallback_cfg.get("enabled"):
            extras = _personal_title_fallback(fresh, shortlist, u,
                                              int(fallback_cfg.get("max_per_user", 5)))
            if extras:
                log.info("用户 %s 个人关键词标题强命中兜底：额外 %d 篇进入精排",
                         slug, len(extras))
                shortlist += extras
        if channel_journals:  # 顶刊通道：绕过关键词得分，按粗筛排序补入通道论文
            picked = {dedup_key(p) for _, p in shortlist}
            extras = []
            for s, p in fresh:
                if len(extras) >= channel_max:
                    break
                if _normalize_journal(p.journal) in channel_journals \
                        and dedup_key(p) not in picked:
                    picked.add(dedup_key(p))
                    extras.append((s, p))
            if extras:
                log.info("用户 %s 顶刊通道：额外 %d 篇进入精排", slug, len(extras))
                shortlist += extras
        shortlists[slug] = shortlist

    # 各用户 shortlist 求并集，LLM 产物全局只做一次（带 SQLite 缓存复用）
    union: dict[str, object] = {}
    for lst in shortlists.values():
        for _, p in lst:
            union.setdefault(dedup_key(p), p)
    log.info("各用户 shortlist 并集 %d 篇，进入全局 AI 处理", len(union))
    artifacts = ensure_artifacts(list(union.values()), llm, conn,
                                 persist=not args.dry_run,
                                 show_translation=email_cfg["show_translation"], log=log,
                                 max_workers=llm.max_workers)

    failed = []
    for slug, u in prepared:
        try:
            deliver(slug, u, shortlists[slug], artifacts, args, email_cfg,
                    scoring_cfg, llm, conn, log,
                    pool_total=len(pool), matched=matched_counts[slug])
        except Exception:
            failed.append(slug)
            log.exception("用户 %s 流程失败，继续下一个用户", slug)
    conn.close()
    log.info("LLM 今日用量：%s", llm.get_usage())
    if failed:
        log.error("以下用户流程失败：%s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
