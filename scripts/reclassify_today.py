"""一次性验证脚本：删除 user001 今日推荐论文的分析缓存并用新 prompt 重分类（落库）。

用法：.venv/bin/python scripts/reclassify_today.py [user_slug]
目的：验证两层分类法——旧缓存无 category/subcategory，强制重跑 LLM 分析以抽查分类质量。
"""

import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import connect, dedup_key  # noqa: E402
from main import apply_lab_profile, load_lab_profile, load_user, USERS_DIR  # noqa: E402
from processing.artifacts import ensure_artifacts  # noqa: E402
from processing.llm import LLMClient  # noqa: E402
from sources.paper import Paper  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reclassify")

slug = sys.argv[1] if len(sys.argv) > 1 else "user001"
day = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
user = apply_lab_profile(load_user(USERS_DIR / f"{slug}.yaml"), load_lab_profile())

conn = connect()
today = day
rows = conn.execute(
    """SELECT p.* FROM recommendations r JOIN papers p ON p.id = r.paper_id
       WHERE r.user_email = ? AND r.sent_date = ?""",
    (user["email"], today)).fetchall()
log.info("今日 %s 推荐论文 %d 篇，删除分析缓存后重跑", slug, len(rows))
conn.execute(
    """DELETE FROM paper_analysis WHERE paper_id IN
       (SELECT paper_id FROM recommendations WHERE user_email = ? AND sent_date = ?)""",
    (user["email"], today))
conn.commit()

papers = [Paper(title=r["title"], abstract=r["abstract"] or "", authors=r["authors"] or "",
                journal=r["journal"] or "", date=r["date"] or "", doi=r["doi"] or "",
                url=r["url"] or "", keywords=[]) for r in rows]
import json  # noqa: E402
for p, r in zip(papers, rows):
    p.keywords = json.loads(r["keywords"] or "[]")

llm = LLMClient()
ensure_artifacts(papers, llm, conn, persist=True,
                 show_translation=False, log=log, max_workers=llm.max_workers)

print("\n==== 分类抽查 ====")
for p in papers:
    pid = conn.execute("SELECT id FROM papers WHERE dedup_key = ?", (dedup_key(p),)).fetchone()["id"]
    ana = conn.execute("SELECT category, subcategory, paper_type FROM paper_analysis WHERE paper_id = ?",
                       (pid,)).fetchone()
    print(f"{ana['category'] or '-':32} {ana['subcategory'] or '-':24} {ana['paper_type'] or '-':4} {p.title[:70]}")
conn.close()
