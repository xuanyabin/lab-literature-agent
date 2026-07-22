"""每日文献情报流水线入口（Phase 1：单用户 MVP）。

流程：PubMed 获取 → 去重 → AI 摘要分析 → 科研新闻生成 → HTML 邮件。

用法：
    python main.py                      # 完整流程并发送邮件
    python main.py --dry-run            # 不发邮件，HTML 写入 logs/
    python main.py --days 3 --limit 15  # 回溯 3 天，最多 15 篇
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import yaml

from mailer.digest_builder import build_digest_html
from mailer.sender import send_email
from processing.analyzer import analyze_paper
from processing.llm import LLMClient
from processing.news_generator import generate_summary
from sources.pubmed import fetch_recent

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"


def load_user(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="每日文献情报流水线")
    parser.add_argument("--user", default=str(BASE_DIR / "config" / "users" / "user001.yaml"),
                        help="用户配置 yaml 路径")
    parser.add_argument("--days", type=int, default=1, help="回溯天数（默认 1）")
    parser.add_argument("--limit", type=int, default=15, help="最多推荐篇数（默认 15）")
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
    papers = papers[: args.limit]
    log.info("进入 AI 处理：%d 篇", len(papers))

    llm = LLMClient()
    items = []
    for i, paper in enumerate(papers, 1):
        log.info("[%d/%d] %s", i, len(papers), paper.title[:60])
        analysis = analyze_paper(paper, llm)
        news = generate_summary(paper, analysis, llm)
        items.append({"paper": paper, "analysis": analysis, "news": news})

    today = date.today().isoformat()
    html = build_digest_html(user["name"], today, items)

    if args.dry_run:
        out = LOG_DIR / f"digest_{today}.html"
        out.write_text(html, encoding="utf-8")
        log.info("dry-run：邮件 HTML 已写入 %s", out)
    else:
        send_email(user["email"], f"每日文献情报 · {today}", html)
        log.info("邮件已发送至 %s", user["email"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
