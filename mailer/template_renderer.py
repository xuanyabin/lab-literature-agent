"""HTML 模板渲染：把 digest_builder 组装好的内容注入 templates/ 下的模板文件。"""

import html
from pathlib import Path
from string import Template

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def render(template_name: str, context: dict) -> str:
    """用 context 替换模板中的 $placeholder 并返回最终 HTML。"""
    template = Template((TEMPLATES_DIR / template_name).read_text(encoding="utf-8"))
    return template.safe_substitute(context)


def escape(text: str) -> str:
    return html.escape(text or "")
