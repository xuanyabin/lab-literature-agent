"""检索词语义拓展：LLM 扩展词自动缓存（每日流程）+ 离线人工审核工具。

每日流程（refresh_auto_terms / apply_auto_terms，V4 粗筛改造）：
    为每个用户维护自动词表 config/users/auto_terms/<slug>.yaml（自动维护，勿手改）：
    - expansion：LLM 为个人检索词生成的同义词/拉丁学名/缩写等扩展词，
      只为召回兜底（不漏文献），等权参与检索与粗筛，不写回个人 yaml；
    - feedback_added：反馈渠道新增的 keywords，等权追加到 keywords。
    触发刷新条件：缓存缺失 / 用户 yaml 有修改 / 缓存超过 7 天；
    LLM 失败时保留旧缓存，不中断流水线。

离线工具（非每日流程，保持人工审核）：
    python -m processing.term_expander config/users/user001.yaml
    打印 aliases 建议到标准输出，人工审核后合并到用户 yaml，工具不改任何配置文件。
"""

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import yaml

from .llm import LLMClient, load_prompt

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
AUTO_TERMS_DIR = BASE_DIR / "config" / "users" / "auto_terms"
AUTO_TERMS_TTL_SECONDS = 7 * 86400
MAX_ALIASES_PER_TERM = 5

TERM_FIELDS = ("research_interest", "keywords", "methods", "species")


def expand_terms(user: dict, llm) -> dict[str, list[str]]:
    """对用户全部检索词做一次语义拓展，返回 {原词: [别名, ...]}。"""
    terms = [(field, t) for field in TERM_FIELDS for t in user.get(field) or [] if t and t.strip()]
    if not terms:
        return {}
    block = "\n".join(f"- [{field}] {t}" for field, t in terms)
    prompt = load_prompt("term_expansion").safe_substitute(terms_block=block)
    return _parse_json(llm.complete(prompt))


def _parse_json(raw: str) -> dict[str, list[str]]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.error("拓展输出不是合法 JSON：%.200s", raw)
        return {}
    return {str(k): [str(a) for a in v] for k, v in data.items() if isinstance(v, list)}


def _auto_path(slug: str, cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{slug}.yaml"


def load_auto_terms(slug: str, cache_dir: Path = AUTO_TERMS_DIR) -> dict:
    """读取自动词表，返回 {"expansion": {...}, "feedback_added": [...]}；缺失/损坏返回空结构。"""
    path = _auto_path(slug, cache_dir)
    if not path.exists():
        return {"expansion": {}, "feedback_added": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "expansion": data.get("expansion") or {},
        "feedback_added": data.get("feedback_added") or [],
    }


def _write_auto_terms(path: Path, expansion: dict, feedback_added: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# 自动维护，请勿手改：LLM 扩展词（仅用于召回，等权）+ 反馈新增关键词\n"
    body = yaml.dump(
        {"updated": date.today().isoformat(), "expansion": expansion,
         "feedback_added": list(feedback_added)},
        allow_unicode=True, sort_keys=False,
    )
    path.write_text(header + body, encoding="utf-8")


def _needs_refresh(cache: Path, user_path: Path | None, now: float) -> bool:
    """刷新触发条件：缓存缺失 / 用户 yaml 比缓存新 / 缓存超过 7 天。"""
    if not cache.exists():
        return True
    cache_mtime = cache.stat().st_mtime
    if user_path is not None and Path(user_path).exists() \
            and Path(user_path).stat().st_mtime > cache_mtime:
        return True
    return now - cache_mtime > AUTO_TERMS_TTL_SECONDS


def refresh_auto_terms(slug: str, user: dict, user_path: Path | None, llm,
                       cache_dir: Path = AUTO_TERMS_DIR) -> dict:
    """按需刷新 LLM 扩展词缓存并返回当前生效的自动词表。

    LLM 调用失败或输出无效时保留旧缓存（不中断流水线）；feedback_added
    字段在刷新时原样保留（由反馈渠道维护）。
    """
    cache = _auto_path(slug, cache_dir)
    if _needs_refresh(cache, user_path, time.time()):
        try:
            expansion = expand_terms(user, llm)
        except Exception:
            logger.warning("扩展词生成异常（用户 %s），沿用旧缓存", slug, exc_info=True)
            expansion = {}
        if expansion:
            expansion = {k: v[:MAX_ALIASES_PER_TERM] for k, v in expansion.items()}
            _write_auto_terms(cache, expansion, load_auto_terms(slug, cache_dir)["feedback_added"])
            logger.info("扩展词缓存已刷新（用户 %s）：%d 个原词", slug, len(expansion))
        elif cache.exists():
            logger.warning("扩展词生成失败（用户 %s），沿用旧缓存", slug)
        else:
            logger.warning("扩展词生成失败且无旧缓存（用户 %s），本次不使用扩展词", slug)
    return load_auto_terms(slug, cache_dir)


def apply_auto_terms(user: dict, auto: dict) -> dict:
    """把自动词表并入用户配置副本（不修改用户原始 yaml）。

    - expansion 并入 aliases：个人别名按键优先；同一原词下个人别名在前，
      扩展词在后，大小写不敏感去重；
    - feedback_added 追加到 keywords：大小写不敏感去重。
    扩展词与个人词等权（打分/检索对 aliases 变体天然等权）。
    """
    merged = dict(user)
    personal = user.get("aliases") or {}
    aliases = {}
    for term, autos in (auto.get("expansion") or {}).items():
        merged_list = list(personal.get(term) or [])
        seen = {str(a).lower() for a in merged_list}
        for a in autos or []:
            if a and str(a).lower() not in seen:
                seen.add(str(a).lower())
                merged_list.append(a)
        aliases[term] = merged_list
    for term, mine in personal.items():
        aliases.setdefault(term, list(mine))
    merged["aliases"] = aliases

    keywords = list(user.get("keywords") or [])
    seen = {str(k).lower() for k in keywords}
    for t in auto.get("feedback_added") or []:
        if t and str(t).lower() not in seen:
            seen.add(str(t).lower())
            keywords.append(t)
    merged["keywords"] = keywords
    return merged


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python -m processing.term_expander <用户yaml路径>", file=sys.stderr)
        return 1
    user = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
    aliases = expand_terms(user, LLMClient())
    if not aliases:
        print("未生成任何别名（输出解析失败或用户无检索词）", file=sys.stderr)
        return 1
    print("# 以下别名建议由 LLM 生成，请审核后合并到用户 yaml 的 aliases 字段：")
    print(yaml.dump({"aliases": aliases}, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
