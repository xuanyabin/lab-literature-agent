"""统一 Paper 对象：所有文献来源（PubMed、bioRxiv……）的输出都转换为该结构。"""

from dataclasses import dataclass, field
import re


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
    # 来源自带的文献类型（如 PubMed PublicationType: "Review" / "Journal Article"）。
    # 抓取侧能拿到就填；拿不到保持空列表，analyzer 再交给 LLM 判断。
    publication_types: list[str] = field(default_factory=list)


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
        for variant in [term, *_alias_values(aliases.get(term))]:
            variant = str(variant).strip()
            if variant and variant.lower() not in seen:
                seen.add(variant.lower())
                out.append(variant)
    return out


def _alias_values(value) -> list:
    """Normalize alias config values.

    YAML entries like ``ants: social insects`` load as a string; treating that
    string as an iterable expands it into single-character aliases. Keep string
    values as one alias while preserving the existing list-style schema.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def variants_for(term: str, aliases: dict | None) -> list[str]:
    """Return the original term plus aliases, normalized and deduped."""
    aliases = aliases or {}
    seen: set[str] = set()
    out: list[str] = []
    for variant in [term, *_alias_values(aliases.get(term))]:
        variant = str(variant).strip().lower()
        if variant and variant not in seen:
            seen.add(variant)
            out.append(variant)
    return out


def _term_pattern(term: str) -> re.Pattern | None:
    """Compile a boundary-aware pattern for a search term.

    Matching is case-insensitive and treats punctuation/hyphen/space as
    separators, so ``single-cell`` matches ``single cell``. Boundaries prevent
    short terms such as ``ant``/``ants`` from matching ``plants`` or
    ``participants``.
    """
    tokens = re.findall(r"[a-z0-9]+", str(term).lower())
    if not tokens:
        return None
    pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(re.escape(t) for t in tokens) + r"(?![a-z0-9])"
    return re.compile(pattern)


def term_count(text: str, term: str) -> int:
    pattern = _term_pattern(term)
    if not pattern:
        return 0
    return len(pattern.findall(text.lower()))


def term_matches(text: str, term: str) -> bool:
    return term_count(text, term) > 0


def any_term_matches(text: str, terms) -> bool:
    return any(term_matches(text, term) for term in terms or [])
