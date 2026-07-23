#!/bin/bash
# 每日流水线：先收集反馈并学习，再跑主推荐。
# 两步故意用顺序执行而非 &&：反馈收集失败（如邮箱 IMAP 临时不可用）
# 不应阻断当天的文献推送。
cd "$(dirname "$0")"
{
  echo "===== $(date '+%F %T') feedback ====="
  .venv/bin/python -m feedback
  echo "===== $(date '+%F %T') main ====="
  .venv/bin/python main.py
  echo "===== $(date '+%F %T') done ====="
} >> logs/cron.log 2>&1
