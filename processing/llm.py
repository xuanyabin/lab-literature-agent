"""LLM 调用封装：模型配置来自 config/model.yaml，API Key 来自 .env（禁止硬编码）。"""

import json
import logging
import os
import threading
import time
from datetime import date
from pathlib import Path
from string import Template

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
DEFAULT_MODEL_CONFIG = BASE_DIR / "config" / "model.yaml"
DEFAULT_USAGE_PATH = BASE_DIR / "logs" / ".llm_usage.json"

# 可重试错误的退避秒数（与 sources/pubmed.py 的 429 退避风格一致）
_RETRY_BACKOFF = (5, 10, 20)


def load_prompt(name: str) -> Template:
    """从 prompts/ 目录加载独立 Prompt 文件（$placeholder 占位符）。"""
    return Template((PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8"))


class BudgetExhaustedError(RuntimeError):
    """LLM 日预算耗尽：调用方应快速失败（不发空壳邮件），次日自动重置。"""


def _is_retryable(exc: Exception) -> bool:
    """429 / 连接错误 / 超时 / 5xx 可重试；其余（如 400 参数错误）直接抛出。"""
    from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    return isinstance(exc, APIStatusError) and exc.status_code >= 500


class LLMClient:
    """最小封装：输入 prompt 字符串，返回模型输出字符串。

    护栏（config/model.yaml，均有默认值）：
      timeout      单次请求超时（默认 60s）
      max_tokens   单次最大输出 token（默认 2000）
      max_retries  可重试错误按 5s/10s/20s 指数退避重试（默认 3 次）
      max_workers  LLM 并发线程数（默认 8；供产物生成/精排批处理读取，
                   complete() 本身无状态、可安全并发调用）
      daily_budget 跨进程日调用预算（默认 1000，≤0 不限；用量记在 logs/.llm_usage.json，
                   跨日自动清零；预算耗尽后 complete() 直接抛错，避免费用失控）

    用量文件同时累计 token 消耗（响应 usage 字段，网关缺字段时按 0 计）：
      {"date", "calls", "prompt_tokens", "completion_tokens", "total_tokens"}
    并发调用下用量读写由类级锁保护（类级：同一用量文件跨实例也安全）。
    """

    _usage_lock = threading.Lock()

    def __init__(self, config_path: Path = DEFAULT_MODEL_CONFIG,
                 usage_path: Path = DEFAULT_USAGE_PATH):
        load_dotenv()
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        self.model = cfg.get("model", "")
        self.temperature = cfg.get("temperature", 0.2)
        self.timeout = cfg.get("timeout", 60)
        self.max_tokens = cfg.get("max_tokens", 2000)
        self.max_retries = cfg.get("max_retries", 3)
        self.max_workers = int(cfg.get("max_workers", 8))
        self.daily_budget = cfg.get("daily_budget", 1000)
        self.usage_path = Path(usage_path)

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY：请在项目根目录 .env 中填写后重试")

        # 支持自定义 OpenAI 兼容网关（如公司统一网关）；不设置则用 SDK 默认官方地址
        base_url = os.environ.get("OPENAI_BASE_URL") or None

        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=self.timeout)

    def complete(self, prompt: str) -> str:
        self._check_budget()
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        # 参数兼容兜底：部分模型/网关不接受 max_tokens 或 temperature 参数，
        # 按报错提示换参数名（max_tokens→max_completion_tokens）或去参（temperature）
        # 后重试；每种参数只兜底一次（兜底后该参数已不在 kwargs 中），其余错误直接抛出
        while True:
            try:
                resp = self._create(**kwargs)
                break
            except Exception as exc:
                msg = str(exc).lower()
                if "max_tokens" in kwargs and "max_tokens" in msg:
                    logger.warning("模型拒绝 max_tokens 参数，改用 max_completion_tokens 重试")
                    kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                elif "temperature" in kwargs and "temperature" in msg:
                    logger.warning("模型拒绝 temperature 参数，去掉后重试")
                    del kwargs["temperature"]
                else:
                    raise
        self._record_call(resp)
        return (resp.choices[0].message.content or "").strip()

    def _create(self, **kwargs):
        """带退避重试的 create 调用：可重试错误按 _RETRY_BACKOFF 退避后重试。"""
        for attempt in range(self.max_retries + 1):
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                if not _is_retryable(exc) or attempt >= self.max_retries:
                    raise
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                logger.warning("LLM 调用失败（%s），%ds 后重试（第 %d/%d 次）",
                               exc, wait, attempt + 1, self.max_retries)
                time.sleep(wait)

    def _check_budget(self) -> None:
        if self.daily_budget <= 0:
            return
        with self._usage_lock:
            used = self._usage().get("calls", 0)
        if used >= self.daily_budget:
            raise BudgetExhaustedError(
                f"LLM 日预算已用完（{used}/{self.daily_budget}），次日自动重置；"
                f"如需继续请调大 config/model.yaml 的 daily_budget")

    def _record_call(self, resp) -> None:
        """累计调用次数与 token 消耗（响应 usage 字段，缺失按 0 计）；并发下由锁保护。"""
        if self.daily_budget <= 0:
            return
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)
        with self._usage_lock:
            data = self._usage()
            data["calls"] = data.get("calls", 0) + 1
            data["prompt_tokens"] = data.get("prompt_tokens", 0) + prompt_tokens
            data["completion_tokens"] = data.get("completion_tokens", 0) + completion_tokens
            data["total_tokens"] = data.get("total_tokens", 0) + total_tokens
            try:
                self.usage_path.parent.mkdir(parents=True, exist_ok=True)
                self.usage_path.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                logger.warning("LLM 用量记录写入失败：%s", self.usage_path, exc_info=True)

    def get_usage(self) -> dict:
        """今日用量（含 token 累计），供流水线结束时汇报；字段缺省补 0。"""
        with self._usage_lock:
            data = self._usage()
        return {"date": data["date"], "calls": data.get("calls", 0),
                "prompt_tokens": data.get("prompt_tokens", 0),
                "completion_tokens": data.get("completion_tokens", 0),
                "total_tokens": data.get("total_tokens", 0)}

    def _usage(self) -> dict:
        """读取今日用量 {"date", "calls", token 累计}；跨日自动清零，文件缺失/损坏按 0 计。"""
        today = date.today().isoformat()
        try:
            data = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("date") != today:
            data = {"date": today, "calls": 0}
        return data
