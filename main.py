"""每日文献情报流水线入口（Phase 1.5：三段式 Daily Literature Intelligence Report）。

流程：PubMed 获取（严格/宽松降级检索）→ 规则粗筛打分 → 去重（含数据库跨天去重）
      → AI 摘要分析 → 新闻摘要 → 中文翻译 → 每日价值总结 → HTML 邮件；
      论文、分析结果与新闻摘要入库 SQLite。

用法：
    python main.py                      # 完整流程并发送邮件
    python main.py --dry-run            # 不发邮件，HTML 写入 logs/
    python main.py --days 3 --limit 15  # 回溯 3 天，最多 15 篇（默认篇数见 config/email.yaml）
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import yaml

from database.db import connect, dedup_key, get_seen_keys, save_analysis, save_news_summary, save_paper
from mailer.digest_builder import build_digest_html, load_email_config
from mailer.sender import send_email
from processing.analyzer import analyze_paper
from processing.daily_summary_generator import generate_daily_summary
from processing.llm import LLMClient
from processing.paper_news_generator import generate_summary
from processing.translator import translate_paper
from recommendation.scorer import load_scoring_config, rank_papers
from sources.pubmed import fetch_recent

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

# 推荐等级占位值：个性化评分在 Phase 4 接入前，所有论文统一为 Reference
PLACEHOLDER_CATEGORY = "Reference"


def load_user(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> int:
    email_cfg = load_email_config()

    parser = argparse.ArgumentParser(description="每日文献情报流水线")
    parser.add_argument("--user", default=str(BASE_DIR / "config" / "users" / "user001.yaml"),
                        help="用户配置 yaml 路径")
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

    user = load_user(Path(args.user))
    log.info("用户：%s <%s>", user["name"], user["email"])

    papers = fetch_recent(user, days=args.days)
    log.info("PubMed 获取 %d 篇（去重后）", len(papers))
    if not papers:
        log.info("今日无新论文，流程结束")
        return 0

    scored = rank_papers(papers, user, load_scoring_config())
    if scored:
        log.info("规则粗筛：候选分数区间 %d–%d", scored[-1][0], scored[0][0])

    conn = connect()
    seen = get_seen_keys(conn)
    fresh = [(s, p) for s, p in scored if dedup_key(p) not in seen]
    if len(fresh) < len(scored):
        log.info("跨天去重：%d 篇已在历史邮件中出现过，跳过", len(scored) - len(fresh))
    papers = [p for _, p in fresh[: args.limit]]
    if not papers:
        log.info("今日检索结果均为历史已发论文，流程结束")
        return 0
    log.info("进入 AI 处理：%d 篇", len(papers))

    persist = not args.dry_run  # dry-run 不写库，避免把未发送的论文标记为已发
    llm = LLMClient()
    items = []
    for i, paper in enumerate(papers, 1):
        log.info("[%d/%d] %s", i, len(papers), paper.title[:60])
        analysis = analyze_paper(paper, llm)
        news = generate_summary(paper, analysis, llm)
        if persist:
            paper_id = save_paper(conn, paper)
            save_analysis(conn, paper_id, analysis)
            save_news_summary(conn, paper_id, news)
        translation = translate_paper(paper, llm) if email_cfg["show_translation"] else {}
        items.append({
            "paper": paper,
            "analysis": analysis,
            "news": news,
            "title_zh": translation.get("title_zh", ""),
            "abstract_zh": translation.get("abstract_zh", ""),
            "category": PLACEHOLDER_CATEGORY,
        })
    conn.close()

    log.info("生成今日价值总结")
    daily_summary = generate_daily_summary(items, llm)

    today = date.today().isoformat()
    html = build_digest_html(user["name"], today, items, daily_summary, email_cfg)

    if args.dry_run:
        out = LOG_DIR / f"digest_{today}.html"
        out.write_text(html, encoding="utf-8")
        log.info("dry-run：邮件 HTML 已写入 %s", out)
    else:
        send_email(user["email"], f"Daily Literature Intelligence Report · {today}", html)
        log.info("邮件已发送至 %s", user["email"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
