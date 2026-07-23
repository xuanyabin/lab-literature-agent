"""每日文献情报流水线入口（Phase 5：反馈学习系统）。

流程：遍历 config/users/ 下所有 active 用户，逐人执行——加载反馈学习词表
      （Phase 5，与手配词表分离，参与检索与粗筛）→ PubMed 获取
      （严格/宽松降级检索）→ 规则粗筛打分选出当日候选
      （实验室公共方向词叠加个人词表）→ 按用户跨天去重
      → AI 摘要分析 → 新闻摘要 → 中文翻译
      → 个性化精排（六维加权 Final Score + AI 推荐理由，按配额定级
      Must Read / Important / Reference）→ 每日价值总结 → HTML 邮件
      （卡片带反馈链接，回信由 python -m feedback 收集学习）；
      论文与分析结果入库 SQLite，推荐记录写入 recommendations 表
      （用户之间去重互不影响）。

用法：
    python main.py                      # 对所有 active 用户执行完整流程并发送邮件
    python main.py --dry-run            # 不发邮件，HTML 写入 logs/
    python main.py --user user001       # 只对指定用户执行（调试用）
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

from database.db import (
    connect, dedup_key, get_seen_keys, save_analysis, save_news_summary,
    save_paper, save_recommendation,
)
from feedback.vocab import load_active_terms
from mailer.digest_builder import build_digest_html, load_email_config
from mailer.sender import send_email
from processing.analyzer import analyze_paper
from processing.daily_summary_generator import generate_daily_summary
from processing.llm import LLMClient
from processing.paper_news_generator import generate_summary
from processing.translator import translate_paper
from recommendation.scorer import load_scoring_config, rank_papers
from recommendation.ranker import load_ranker_weights, rank_items
from sources.pubmed import fetch_recent

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
    """把实验室公共方向并入用户配置副本：lab_topics 参与打分，别名表合并（个人优先）。"""
    merged = dict(user)
    merged["lab_topics"] = list(lab.get("topics") or [])
    merged["aliases"] = {**(lab.get("aliases") or {}), **(user.get("aliases") or {})}
    return merged


def run_for_user(slug: str, user: dict, args: argparse.Namespace,
                 email_cfg: dict, log: logging.Logger) -> None:
    """单用户完整流水线：检索 → 打分定级 → 去重 → AI 处理 → 生成并投递邮件。"""
    log.info("用户：%s <%s>", user["name"], user["email"])

    conn = connect()
    user = dict(user)
    user["learned_terms"] = load_active_terms(conn, user["email"])
    if user["learned_terms"]:
        log.info("学习词表：%d 个有效词参与检索与打分", len(user["learned_terms"]))

    papers = fetch_recent(user, days=args.days)
    log.info("PubMed 获取 %d 篇（去重后）", len(papers))
    if not papers:
        log.info("今日无新论文，流程结束")
        conn.close()
        return

    scoring_cfg = load_scoring_config()
    scored = rank_papers(papers, user, scoring_cfg)
    if scored:
        log.info("规则粗筛：候选分数区间 %d–%d", scored[-1][0], scored[0][0])

    seen = get_seen_keys(conn, user["email"])
    fresh = [(s, p) for s, p in scored if dedup_key(p) not in seen]
    if len(fresh) < len(scored):
        log.info("跨天去重：%d 篇已在历史邮件中出现过，跳过", len(scored) - len(fresh))
    shortlist = fresh[: args.limit]
    if not shortlist:
        log.info("今日检索结果均为历史已发论文，流程结束")
        conn.close()
        return
    log.info("进入 AI 处理：%d 篇", len(shortlist))

    llm = LLMClient()
    items = []
    for i, (score, paper) in enumerate(shortlist, 1):
        log.info("[%d/%d] (粗筛 %d 分) %s", i, len(shortlist), score, paper.title[:60])
        analysis = analyze_paper(paper, llm)
        news = generate_summary(paper, analysis, llm)
        translation = translate_paper(paper, llm) if email_cfg["show_translation"] else {}
        items.append({
            "paper": paper,
            "analysis": analysis,
            "news": news,
            "title_zh": translation.get("title_zh", ""),
            "abstract_zh": translation.get("abstract_zh", ""),
        })

    log.info("个性化精排：六维加权 Final Score + 生成推荐理由")
    items = rank_items(items, user, llm, scoring_cfg["journal_tiers"],
                       load_ranker_weights(), scoring_cfg["tiers"])
    n_must = sum(1 for it in items if it["category"] == "Must Read")
    n_important = sum(1 for it in items if it["category"] == "Important")
    log.info("精排定级：Must Read %d / Important %d / Reference %d",
             n_must, n_important, len(items) - n_must - n_important)

    persist = not args.dry_run  # dry-run 不写库，避免把未发送的论文标记为已发
    today = date.today().isoformat()
    if persist:
        for it in items:
            paper_id = save_paper(conn, it["paper"])
            it["paper_id"] = paper_id
            save_analysis(conn, paper_id, it["analysis"])
            save_news_summary(conn, paper_id, it["news"])
            save_recommendation(conn, user["email"], paper_id, it["category"], it["score"], today)
    conn.close()

    log.info("生成今日价值总结")
    daily_summary = generate_daily_summary(items, llm)

    html = build_digest_html(user["name"], today, items, daily_summary, email_cfg,
                             user_email=user["email"])

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
    email_cfg["feedback_email"] = os.environ.get("DIGEST_FROM_EMAIL", "")

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
    for slug, user in users:
        try:
            run_for_user(slug, apply_lab_profile(user, lab), args, email_cfg, log)
        except Exception:
            log.exception("用户 %s 流程失败，继续下一个用户", slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
