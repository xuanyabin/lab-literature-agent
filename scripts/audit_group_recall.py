"""lab.yaml 分组召回覆盖审计：逐词查 PubMed 近 N 天命中数，标记死词/过热词。

用途：维护 topic_groups 分组词表时，验证每组的召回是否"足够覆盖"——
  DEAD = 近 N 天零命中（词可能太窄或拼写问题，建议删词或换写法）
  HOT  = 命中数超过 --hot 阈值（召回过多，建议移入 rank_only 只打分不召回）
rank_only 组一并审计（它本就该热，HOT 标记仅作参考，不算异常）。

用法（项目根目录执行）：
    .venv/bin/python scripts/audit_group_recall.py                          # 全部组
    .venv/bin/python scripts/audit_group_recall.py --group core_spatial_omics
    .venv/bin/python scripts/audit_group_recall.py --days 30 --hot 100
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_lab_profile  # noqa: E402
from sources import pubmed  # noqa: E402

DEAD = "DEAD"
HOT = "HOT"


def classify(count: int, hot: int) -> str:
    """命中数 → 标记：0 → DEAD；> hot → HOT；否则空串。"""
    if count <= 0:
        return DEAD
    if count > hot:
        return HOT
    return ""


def audit_groups(lab: dict, days: int, hot: int, count_fn, only_group: str = None) -> dict:
    """逐组逐词审计，返回 {组名: [(词, 命中数, 标记)]}。

    count_fn(term, days) -> int，可注入 mock；rank_only 作为伪组附在最后。
    """
    groups = dict(lab.get("topic_groups") or {})
    groups["rank_only"] = list(lab.get("rank_only") or [])
    if only_group:
        if only_group not in groups:
            raise ValueError(
                f"未知分组 {only_group!r}，可选：{', '.join(groups)}"
            )
        groups = {only_group: groups[only_group]}
    results = {}
    for name, terms in groups.items():
        rows = []
        for term in terms or []:
            count = count_fn(term, days)
            rows.append((term, count, classify(count, hot)))
        results[name] = rows
    return results


def render(results: dict, days: int, hot: int) -> str:
    lines = []
    total_dead, total_hot = [], []
    for name, rows in results.items():
        lines.append(f"\n== {name}（{len(rows)} 词，近 {days} 天）==")
        for term, count, flag in rows:
            mark = f"  <-- {flag}" if flag else ""
            lines.append(f"  {count:>7}  {term}{mark}")
            if flag == DEAD:
                total_dead.append((name, term))
            elif flag == HOT:
                total_hot.append((name, term))
    lines.append("\n== 汇总 ==")
    if total_dead:
        lines.append(f"DEAD（零命中，建议删词/换写法）{len(total_dead)} 个：")
        lines += [f"  [{g}] {t}" for g, t in total_dead]
    if total_hot:
        lines.append(f"HOT（> {hot} 命中，建议移入 rank_only）{len(total_hot)} 个：")
        lines += [f"  [{g}] {t}" for g, t in total_hot]
    if not total_dead and not total_hot:
        lines.append("全部词命中数正常。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="lab.yaml 分组召回覆盖审计")
    parser.add_argument("--days", type=int, default=90, help="统计近 N 天（默认 90）")
    parser.add_argument("--hot", type=int, default=150, help="HOT 阈值（默认 150）")
    parser.add_argument("--group", default=None, help="只审计指定分组（含 rank_only）")
    parser.add_argument("--delay", type=float, default=0.4,
                        help="每次请求间隔秒数，防 NCBI 限流（默认 0.4）")
    args = parser.parse_args()

    lab = load_lab_profile()

    def count_fn(term: str, days: int) -> int:
        time.sleep(args.delay)
        return pubmed.count_pmids(f'"{term}"', days)

    results = audit_groups(lab, args.days, args.hot, count_fn, only_group=args.group)
    print(render(results, args.days, args.hot))


if __name__ == "__main__":
    main()
