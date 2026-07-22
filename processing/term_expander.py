"""检索词语义拓展工具（离线运行，非每日流程）。

用法：
    python -m processing.term_expander config/users/user001.yaml

用 LLM 为用户的每个检索词生成同义词/拉丁学名/缩写等别名建议，
以 yaml 片段形式打印到标准输出。请人工审核后合并到用户 yaml 的 aliases 字段，
工具本身不修改任何配置文件。
"""

import json
import logging
import sys
from pathlib import Path

import yaml

from .llm import LLMClient, load_prompt

logger = logging.getLogger(__name__)

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
