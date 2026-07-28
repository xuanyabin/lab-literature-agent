#!/bin/bash
# 每周情报报告：聚合 SQLite 中最近 7 天的推荐记录生成周报并投递。
# 建议 cron 每周一早上执行，如：53 7 * * 1 /path/to/run_weekly.sh
cd "$(dirname "$0")"
# 节假日判断：当天是法定节假日则当周周报跳过。
if [ "$(.venv/bin/python -m scheduler.holiday)" = "0" ]; then
  echo "===== $(date '+%F %T') holiday skip =====" >> logs/cron.log 2>&1
  exit 0
fi
{
  echo "===== $(date '+%F %T') weekly ====="
  .venv/bin/python weekly_report.py
  echo "===== $(date '+%F %T') done ====="
} >> logs/cron.log 2>&1
