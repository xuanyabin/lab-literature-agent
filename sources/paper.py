"""统一 Paper 对象：所有文献来源（PubMed、bioRxiv……）的输出都转换为该结构。"""

from dataclasses import dataclass, field


@dataclass
class Paper:
    title: str
    abstract: str
    authors: str
    journal: str
    date: str
    doi: str
    url: str
    keywords: list[str] = field(default_factory=list)


def expand_with_aliases(terms, aliases: dict | None) -> list[str]:
    """把检索词与其 aliases（语义拓展词）合并为去重后的列表，原词在前。

    用户 yaml 中 aliases 的键必须与检索词原文一致，例如：
        species: [honeybee]
        aliases: {honeybee: ["Apis mellifera", "Apis"]}
    """
    aliases = aliases or {}
    seen: set[str] = set()
    out: list[str] = []
    for term in terms or []:
        for variant in [term, *aliases.get(term, [])]:
            variant = str(variant).strip()
            if variant and variant.lower() not in seen:
                seen.add(variant.lower())
                out.append(variant)
    return out
