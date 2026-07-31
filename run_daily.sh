#!/bin/bash
# 每日流水线：先收集反馈并学习，再跑主推荐。
# 两步故意用顺序执行而非 &&：反馈收集失败（如邮箱 IMAP 临时不可用）
# 不应阻断当天的文献推送。
cd "$(dirname "$0")"
# 节假日判断：0 表示今天是法定节假日，整天跳过（反馈收集也一并跳过，
# 节后首次运行会收到累积的反馈回信）；否则 DAYS 为回溯天数（覆盖节假日空窗）。
DAYS=$(.venv/bin/python -m scheduler.holiday)
if [ "$DAYS" = "0" ]; then
  echo "===== $(date '+%F %T') holiday skip =====" >> logs/cron.log 2>&1
  exit 0
fi
{
  echo "===== $(date '+%F %T') feedback ====="
  .venv/bin/python -m feedback
  echo "===== $(date '+%F %T') main ====="
  .venv/bin/python main.py --days "$DAYS"
  echo "===== $(date '+%F %T') done ====="
} >> logs/cron.log 2>&1
