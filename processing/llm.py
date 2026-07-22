"""LLM 调用封装：模型配置来自 config/model.yaml，API Key 来自 .env（禁止硬编码）。"""

import logging
import os
from pathlib import Path
from string import Template

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DEFAULT_MODEL_CONFIG = BASE_DIR / "config" / "model.yaml"


def load_prompt(name: str) -> Template:
    """从 prompts/ 目录加载独立 Prompt 文件（$placeholder 占位符）。"""
    return Template((PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8"))


class LLMClient:
    """最小封装：输入 prompt 字符串，返回模型输出字符串。"""

    def __init__(self, config_path: Path = DEFAULT_MODEL_CONFIG):
        load_dotenv()
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        self.model = cfg.get("model", "")
        self.temperature = cfg.get("temperature", 0.2)

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY：请在项目根目录 .env 中填写后重试")

        # 支持自定义 OpenAI 兼容网关（如公司统一网关）；不设置则用 SDK 默认官方地址
        base_url = os.environ.get("OPENAI_BASE_URL") or None

        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, prompt: str) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            # 部分模型不接受 temperature 参数，遇到此类报错时去掉该参数重试一次
            if "temperature" not in str(exc).lower():
                raise
            logger.warning("模型拒绝 temperature 参数，去掉后重试")
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
        return (resp.choices[0].message.content or "").strip()
