"""论文 → lab.yaml topic_groups 模块归属（渲染时计算，不落库）。

规则：
- 对每篇论文文本（标题 + 摘要 + 关键词小写拼接）逐组统计命中词数，
  组内词命中判定复用粗筛同款的 term_matches（含 aliases 展开）；
- 命中多个组时取命中词数最多者为主模块；持平取用户订阅组优先；
  再持平取 topic_groups 中靠前的组；
- 未命中任何组归入 "其他"。
"""

from sources.paper import term_matches, variants_for

#: 未命中任何组时的兜底模块名（group_labels 里通常不配置，直接显示原文）。
OTHER = "其他"


def assign_module(text: str, topic_groups: dict | None,
                  subscribed: list | None = None,
                  aliases: dict | None = None) -> str:
    """返回 text 命中的主模块组名；未命中返回 OTHER。

    text 由调用方拼接（建议标题 + 摘要 + 关键词），大小写不敏感。
    subscribed 为用户订阅组名列表（含 default_groups 展开结果）。
    """
    if not topic_groups:
        return OTHER
    subscribed_set = set(subscribed or [])
    best_name = OTHER
    best_key = None
    for order, (name, terms) in enumerate(topic_groups.items()):
        hits = sum(
            1 for term in terms or []
            if any(term_matches(text, v) for v in variants_for(str(term), aliases))
        )
        if hits <= 0:
            continue
        # 命中多者胜 → 订阅组优先 → 组序靠前者胜
        key = (hits, 1 if name in subscribed_set else 0, -order)
        if best_key is None or key > best_key:
            best_key = key
            best_name = name
    return best_name


def group_label(labels: dict | None, name: str) -> str:
    """组名 → 中文显示名；缺省回退组名本身。"""
    return str((labels or {}).get(name) or name)
