"""每周情报报告入口（Phase 6）。

流程：遍历 config/users/ 下所有 active 用户，逐人从 SQLite 聚合最近 N 天
      推荐记录（recommendations ⋈ papers ⋈ paper_news_summary，不重新检索分析）——
      分布统计（定级 / 期刊分层 / Top 期刊 / 高频关键词，纯数据）
      + 阅读趋势（窗口内反馈正/中/负分桶 + 当前有效学习词 Top）
      → LLM 周度趋势总结 → HTML 周报邮件。
      清单与趋势总结分两种模式（按用户全或无）：窗口内推荐任意一篇有星级标注
      → 星级模式（只收最新标注 ≥3 星、模块内按星级降序、总结侧重 4–5 星）；
      完全无标注 → 维持 Must Read / Important 清单与原趋势总结。

用法：
    python weekly_report.py                 # 对所有 active 用户生成周报并发送邮件
    python weekly_report.py --dry-run       # 不发邮件，HTML 写入 logs/
    python weekly_report.py --user user001  # 只对指定用户执行（调试用）
    python weekly_report.py --days 7        # 回溯天数（默认 7）
    python weekly_report.py --days 30       # 月报：近 30 天阅读趋势 + 领域文献总结
"""

import argparse
import logging
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from database.db import (
    connect, get_analysis, get_feedback_since, get_latest_ratings,
    get_week_recommendations,
)
from feedback.vocab import load_active_terms
from mailer.sender import send_email
from mailer.weekly_builder import build_weekly_html
from main import LOG_DIR, USERS_DIR, load_user, load_users
from processing import taxonomy
from processing.llm import LLMClient
from processing.weekly_stats import compute_reading_trends, compute_stats, parse_star
from processing.weekly_summary_generator import generate_weekly_summary
from recommendation.scorer import load_journal_tiers


def run_for_user(slug: str, user: dict, args: argparse.Namespace,
                 log: logging.Logger) -> None:
    """单用户周报/月报：聚合窗口内推荐记录 → 统计 + 趋势总结 → 生成并投递邮件。"""
    label = "月报" if args.days >= 28 else "周报"
    log.info("用户：%s <%s>", user["name"], user["email"])

    today = date.today().isoformat()
    since = (date.today() - timedelta(days=args.days)).isoformat()
    conn = connect()
    rows = get_week_recommendations(conn, user["email"], since)
    if not rows:
        conn.close()
        log.info("最近 %d 天无推荐记录，跳过", args.days)
        return
    log.info("聚合 %s ~ %s 推荐记录：%d 篇", since, today, len(rows))
    feedback_rows = get_feedback_since(conn, user["email"], since)
    active_terms = load_active_terms(conn, user["email"])

    # 展示分类 + 文献类型 badge：category/subcategory 与 paper_type 均读分析缓存
    # （LLM 分析时已按 taxonomy 校验落库）；无分类的旧缓存归入"其他"分区
    rows = [dict(r) for r in rows]
    for row in rows:
        ana = get_analysis(conn, row["paper_id"]) or {}
        cat = ana.get("category") or ""
        row["category_key"] = cat
        row["subcategory_label"] = taxonomy.subcategory_label(cat, ana.get("subcategory") or "")
        row["paper_type"] = ana.get("paper_type", "")
    # 每篇论文的最新标注（不限定标注时间窗：周末推送、下周标注也要关联上）；
    # 查询异常按"无标注"回退，不中断流程
    try:
        ratings = get_latest_ratings(conn, user["email"])
    except Exception:
        log.warning("读取用户标注失败，按无标注模式生成报告", exc_info=True)
        ratings = {}
    conn.close()

    # 清单模式（全或无）：窗口内推荐任意一篇存在星级标注 → 星级模式
    #（清单只收 ≥3 星、模块内按星级排序、趋势总结只看 ≥3 星）；
    # 完全无标注 → 维持 Must Read / Important 清单现状
    star_mode = any(parse_star(ratings.get(row["paper_id"])) is not None for row in rows)
    active_ratings = ratings if star_mode else None

    stats = compute_stats(rows, load_journal_tiers())
    trends = compute_reading_trends(feedback_rows, active_terms)

    try:
        trend_summary = generate_weekly_summary(rows, LLMClient(), active_ratings)
    except Exception:
        log.exception("周度趋势总结生成失败，报告中该部分置空")
        trend_summary = ""

    html = build_weekly_html(user["name"], since, today, rows, trend_summary,
                             stats, trends, active_ratings)

    if args.dry_run:
        out = LOG_DIR / f"weekly_{today}_{slug}.html"
        out.write_text(html, encoding="utf-8")
        log.info("dry-run：%s HTML 已写入 %s", label, out)
    else:
        subject_label = "Monthly" if args.days >= 28 else "Weekly"
        send_email(user["email"],
                   f"{subject_label} Literature Intelligence Report · {since} ~ {today}", html)
        log.info("%s已发送至 %s", label, user["email"])


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="每周情报报告")
    parser.add_argument("--user", default=None,
                        help="只执行指定用户（config/users/ 下的文件名，不含 .yaml）；默认执行所有 active 用户")
    parser.add_argument("--days", type=int, default=7, help="回溯天数（默认 7）")
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
    log = logging.getLogger("weekly_report")

    if args.user:
        users = [(args.user, load_user(USERS_DIR / f"{args.user}.yaml"))]
    else:
        users = load_users()
    if not users:
        log.info("没有 active 用户，流程结束")
        return 0
    log.info("本次执行 %d 个用户：%s", len(users), ", ".join(slug for slug, _ in users))

    failed = []
    for slug, user in users:
        try:
            run_for_user(slug, user, args, log)
        except Exception:
            failed.append(slug)
            label = "月报" if args.days >= 28 else "周报"
            log.exception("用户 %s %s失败，继续下一个用户", slug, label)
    if failed:
        log.error("本次执行有 %d 个用户失败：%s", len(failed), ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
