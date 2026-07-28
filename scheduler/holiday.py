"""静态节假日表与节后合并补发（方案 B5）。

config/holidays.yaml 维护中国法定节假日清单（每年初按国务院放假安排手工更新）。
节假日当天跳过推送（backfill_days 返回 0），节后首个工作日返回 1 + 之前
连续节假日天数，供 main.py --days 参数覆盖节假日空窗、合并补发文献。

用法：
    python -m scheduler.holiday    # 打印今天的 backfill_days 整数（供 shell 命令替换）
"""

from datetime import date, timedelta
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
HOLIDAYS_PATH = BASE_DIR / "config" / "holidays.yaml"

# backfill_days 上限：防止节假日表误配导致回溯天数失控
MAX_BACKFILL_DAYS = 10


def load_holidays(path=HOLIDAYS_PATH) -> set[date]:
    """读取节假日清单；文件缺失/为空/无 holidays 键时返回空集合（不报错）。"""
    p = Path(path)
    if not p.exists():
        return set()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    days = data.get("holidays") or []
    return {date.fromisoformat(str(d)) for d in days}


def backfill_days(today: date, holidays: set[date]) -> int:
    """today 是节假日返回 0（当日跳过）；否则返回 1 + 之前连续节假日天数，上限 10。"""
    if today in holidays:
        return 0
    days = 1
    cursor = today - timedelta(days=1)
    while cursor in holidays and days < MAX_BACKFILL_DAYS:
        days += 1
        cursor -= timedelta(days=1)
    return days


def main() -> None:
    """CLI：打印今天的 backfill_days 整数结果（只打印数字，供 shell 命令替换用）。"""
    print(backfill_days(date.today(), load_holidays()))


if __name__ == "__main__":
    main()
