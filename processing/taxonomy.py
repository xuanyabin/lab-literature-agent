"""两层展示分类法（config/taxonomy.yaml）的加载、校验与显示名查询。

分类由 LLM 在逐篇分析时判定（prompts/paper_analysis.txt），随分析缓存落库；
本模块供 analyzer 校验 LLM 输出、渲染层取中文显示名与固定大类顺序。

失败行为：taxonomy.yaml 缺失或损坏时记 error 并返回空配置——validate 对任何
输入都判非法（回退空分类），ordered_categories 返回空列表，渲染层因此把
所有论文归入"其他"分区，流水线不中断。
"""

import logging
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

TAXONOMY_CONFIG = Path(__file__).resolve().parent.parent / "config" / "taxonomy.yaml"


@lru_cache(maxsize=1)
def load_taxonomy(path: Path = TAXONOMY_CONFIG) -> dict:
    """读取分类配置；缺失/损坏/结构不符时记 error 并返回空 dict。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 配置损坏不得中断流水线
        logger.error("taxonomy 配置读取失败（%s）：%s；全部论文按未分类渲染", path, exc)
        return {}
    cats = (data or {}).get("categories")
    if not isinstance(cats, dict) or not cats:
        logger.error("taxonomy 配置缺少 categories（%s）；全部论文按未分类渲染", path)
        return {}
    return {"categories": cats}


def ordered_categories(taxonomy: dict | None = None) -> list[tuple[str, str]]:
    """按配置文件顺序返回 [(大类 key, 大类中文名)]，配置为空时返回 []。"""
    tax = taxonomy if taxonomy is not None else load_taxonomy()
    out = []
    for key, cat in (tax.get("categories") or {}).items():
        if isinstance(cat, dict):
            out.append((key, str(cat.get("label_zh") or key)))
    return out


def validate(category: str, subcategory: str, taxonomy: dict | None = None) -> tuple[str, str]:
    """校验 LLM 输出的 (category, subcategory)。

    两者都为空 = 合法的"未分类"；category 必须是已配置的大类 key 且 subcategory
    必须属于该大类，否则记 warning 并两字段同时回退 ""。
    """
    category = (category or "").strip()
    subcategory = (subcategory or "").strip()
    if not category and not subcategory:
        return "", ""
    tax = taxonomy if taxonomy is not None else load_taxonomy()
    cat = (tax.get("categories") or {}).get(category)
    subs = (cat or {}).get("subcategories") if isinstance(cat, dict) else None
    if subs and subcategory in subs:
        return category, subcategory
    logger.warning("taxonomy: 非法分类回退为空 category=%r subcategory=%r", category, subcategory)
    return "", ""


def subcategory_label(category: str, subcategory: str, taxonomy: dict | None = None) -> str:
    """取子类中文显示名；未配置/不属于该大类时返回 ""（不渲染 badge）。"""
    tax = taxonomy if taxonomy is not None else load_taxonomy()
    cat = (tax.get("categories") or {}).get(category or "")
    subs = (cat or {}).get("subcategories") if isinstance(cat, dict) else None
    if subs and subcategory in subs:
        return str(subs[subcategory])
    return ""
