"""查看 SQLite 中已入库文献的命令行小工具。

papers 表是全局共享表（所有用户抓取的论文都进这张表），两个日期字段：
  first_seen —— 入库时间（哪天抓进库的）
  date       —— 论文发表日期

用法（项目根目录执行）：
    .venv/bin/python scripts/list_papers.py                  # 今日入库的全部文献
    .venv/bin/python scripts/list_papers.py --date 2026-07-29   # 指定入库日期
    .venv/bin/python scripts/list_papers.py --pub-date 2026-07-30  # 按发表日期查
    .venv/bin/python scripts/list_papers.py --days 3         # 最近 3 天入库（按天分组）
    .venv/bin/python scripts/list_papers.py --today --count-only  # 只看数量
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# 允许从项目根目录直接运行本脚本
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import connect  # noqa: E402


def query_papers(conn: sqlite3.Connection, *,
                 seen_date: str | None = None,
                 pub_date: str | None = None,
                 days: int | None = None) -> list[sqlite3.Row]:
    """按入库日期 / 发表日期 / 最近 N 天查询文献，按日期倒序、id 升序返回。"""
    sql = "SELECT title, journal, date, url, substr(first_seen, 1, 10) AS seen FROM papers"
    where, params = [], []
    if pub_date:
        where.append("date = ?")
        params.append(pub_date)
    elif seen_date:
        where.append("substr(first_seen, 1, 10) = ?")
        params.append(seen_date)
    elif days:
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        where.append("substr(first_seen, 1, 10) >= ?")
        params.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY seen DESC, id"
    return conn.execute(sql, params).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 literature_agent.db 中已入库的文献")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--today", action="store_true", help="今日入库（默认）")
    group.add_argument("--date", metavar="YYYY-MM-DD", help="按入库日期查询")
    group.add_argument("--pub-date", metavar="YYYY-MM-DD", help="按发表日期查询")
    group.add_argument("--days", type=int, metavar="N", help="最近 N 天入库（按天分组）")
    parser.add_argument("--count-only", action="store_true", help="只输出数量")
    args = parser.parse_args()

    conn = connect()
    try:
        rows = query_papers(
            conn,
            seen_date=args.date or (date.today().isoformat() if not (args.pub_date or args.days) else None),
            pub_date=args.pub_date,
            days=args.days,
        )
    finally:
        conn.close()

    label = (f"发表日期 {args.pub_date}" if args.pub_date
             else f"最近 {args.days} 天入库" if args.days
             else f"入库日期 {args.date or date.today().isoformat()}")
    print(f"{label}：共 {len(rows)} 篇")
    if args.count_only:
        return 0
    last_seen = None
    for i, r in enumerate(rows, 1):
        if args.days and r["seen"] != last_seen:
            last_seen = r["seen"]
            print(f"\n── {last_seen} ──")
        journal = r["journal"] or "-"
        print(f"{i:3d}. [{r['date'] or '-'}] {r['title']}\n      {journal} | {r['url'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
