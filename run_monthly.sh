#!/bin/bash
# 每月情报报告：聚合 SQLite 中最近 30 天的推荐记录生成月报并投递。
# 建议 cron 每月 1 日早上执行，如：53 7 1 * * /path/to/run_monthly.sh
cd "$(dirname "$0")"
# 节假日判断：当天是法定节假日则当月月报跳过。
if [ "$(.venv/bin/python -m scheduler.holiday)" = "0" ]; then
  echo "===== $(date '+%F %T') holiday skip =====" >> logs/cron.log 2>&1
  exit 0
fi
{
  echo "===== $(date '+%F %T') monthly ====="
  .venv/bin/python weekly_report.py --days 30
  echo "===== $(date '+%F %T') done ====="
} >> logs/cron.log 2>&1
