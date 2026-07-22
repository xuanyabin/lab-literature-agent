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
