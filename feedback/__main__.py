"""`python -m feedback` 入口：收集回信 + 执行学习闭环（建议每日跑在主流水线之前）。"""

import argparse
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"


def main() -> int:
    parser = argparse.ArgumentParser(description="反馈收集与学习闭环")
    parser.add_argument("--learn-only", action="store_true",
                        help="跳过 IMAP 收集，只处理 feedback_data/pending 中待学习的反馈")
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
    log = logging.getLogger("feedback")

    from database.db import connect
    from feedback import store
    from feedback.collector import collect, collect_keyword_queue
    from feedback.learner import learn_from_feedback
    from feedback.vocab import load_learned_config
    from main import apply_lab_profile, load_lab_profile, load_users

    conn = connect()
    try:
        if args.learn_only:
            log.info("learn-only：跳过 IMAP 收集")
        else:
            log.info("收集到 %d 条新反馈", collect(conn))
        log.info("网页端关键词队列：%d 条新增关键词入词表", collect_keyword_queue())

        lab = load_lab_profile()
        users = [(slug, apply_lab_profile(user, lab)) for slug, user in load_users()]
        entries = store.load_pending()
        pending = {u["email"]: [e for e in entries if e["user_email"] == u["email"]]
                   for _, u in users}
        if not any(pending.values()):
            log.info("没有待学习的反馈，流程结束")
            return 0

        from processing.llm import LLMClient

        llm = LLMClient()
        cfg = load_learned_config()
        for slug, user in users:
            if not pending[user["email"]]:
                continue
            stats = learn_from_feedback(conn, user, llm, cfg)
            log.info("用户 %s 学习完成：高分 %d 条，低分 %d 条，跳过 %d 条",
                     slug, stats["positive"], stats["negative"], stats["skipped"])
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
