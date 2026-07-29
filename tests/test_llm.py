"""processing.llm 的护栏测试：退避重试 / 日预算 / temperature 回退（不碰真实 API）。"""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, RateLimitError

from processing.llm import LLMClient, _is_retryable


def _resp(text="ok", usage=None):
    """usage: (prompt_tokens, completion_tokens) 或 None（模拟网关缺 usage 字段）。"""
    ns = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
    if usage is not None:
        ns.usage = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1],
                                   total_tokens=usage[0] + usage[1])
    return ns


def _status_error(status, message="boom"):
    req = httpx.Request("POST", "https://api.test/chat/completions")
    return APIStatusError(message, response=httpx.Response(status, request=req), body=None)


def _rate_limit():
    req = httpx.Request("POST", "https://api.test/chat/completions")
    return RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


class _FakeCompletions:
    """按预设 outcomes 依次抛出异常或返回响应，并记录每次调用参数。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(outcomes, max_retries=2, daily_budget=100, usage_path=None):
    """绕过 __init__（不需要真实 API key）构造带假 OpenAI 客户端的 LLMClient。"""
    c = object.__new__(LLMClient)
    c.model = "test-model"
    c.temperature = 0.2
    c.max_tokens = 100
    c.max_retries = max_retries
    c.daily_budget = daily_budget
    c.usage_path = usage_path or Path("/nonexistent/.llm_usage.json")
    c._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(outcomes)))
    return c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("processing.llm.time.sleep", lambda _: None)


def test_is_retryable_classification():
    assert _is_retryable(_rate_limit())            # 429
    assert _is_retryable(_status_error(500))       # 5xx
    assert _is_retryable(_status_error(503))
    assert not _is_retryable(_status_error(400))   # 参数错误不重试
    assert not _is_retryable(_status_error(401))   # 鉴权错误不重试
    assert not _is_retryable(ValueError("x"))


def test_retry_on_429_then_success():
    c = _client([_rate_limit(), _resp("done")])
    assert c.complete("p") == "done"
    calls = c._client.chat.completions.calls
    assert len(calls) == 2
    # 所有调用都带 max_tokens 与 temperature
    assert all(k["max_tokens"] == 100 for k in calls)
    assert all(k["temperature"] == 0.2 for k in calls)


def test_retry_exhausted_raises():
    c = _client([_rate_limit()] * 3, max_retries=2)
    with pytest.raises(RateLimitError):
        c.complete("p")
    assert len(c._client.chat.completions.calls) == 3  # 1 次原始 + 2 次重试


def test_no_retry_on_400():
    c = _client([_status_error(400, "invalid request")])
    with pytest.raises(APIStatusError):
        c.complete("p")
    assert len(c._client.chat.completions.calls) == 1


def test_temperature_fallback_still_works():
    """模型拒绝 temperature（400）时不进重试循环，直接走去掉该参数的回退。"""
    c = _client([_status_error(400, "unsupported parameter: temperature"), _resp("ok")])
    assert c.complete("p") == "ok"
    calls = c._client.chat.completions.calls
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]
    assert calls[1]["max_tokens"] == 100  # 回退调用仍带 max_tokens


def test_max_tokens_fallback_switches_to_completion_tokens():
    """网关拒绝 max_tokens（400）时换用 max_completion_tokens 重试，其余参数保留。"""
    err = _status_error(400, "Unsupported parameter: 'max_tokens' is not supported "
                             "with this model. Use 'max_completion_tokens' instead.")
    c = _client([err, _resp("ok")])
    assert c.complete("p") == "ok"
    calls = c._client.chat.completions.calls
    assert calls[0]["max_tokens"] == 100
    assert "max_tokens" not in calls[1]
    assert calls[1]["max_completion_tokens"] == 100
    assert calls[1]["temperature"] == 0.2


def test_max_tokens_then_temperature_fallback():
    """两种参数都被拒时依次兜底：先换 max_completion_tokens，再去 temperature。"""
    err1 = _status_error(400, "Unsupported parameter: 'max_tokens'")
    err2 = _status_error(400, "Unsupported parameter: 'temperature'")
    c = _client([err1, err2, _resp("ok")])
    assert c.complete("p") == "ok"
    calls = c._client.chat.completions.calls
    assert "max_completion_tokens" in calls[1]
    assert "max_tokens" not in calls[2]
    assert "temperature" not in calls[2]


def test_max_tokens_fallback_only_once():
    """换参后仍报 max_tokens 错误时不再兜底，直接抛出（不死循环）。"""
    err = _status_error(400, "Unsupported parameter: 'max_tokens'")
    c = _client([err, err])
    with pytest.raises(APIStatusError):
        c.complete("p")
    assert len(c._client.chat.completions.calls) == 2  # 原始 1 次 + 兜底 1 次


def test_budget_exceeded_blocks_call(tmp_path):
    usage = tmp_path / ".llm_usage.json"
    usage.write_text(json.dumps({"date": date.today().isoformat(), "calls": 5}))
    c = _client([_resp()], daily_budget=5, usage_path=usage)
    with pytest.raises(RuntimeError, match="日预算"):
        c.complete("p")
    assert len(c._client.chat.completions.calls) == 0  # 根本没发请求


def test_budget_counts_and_resets_across_days(tmp_path):
    usage = tmp_path / ".llm_usage.json"
    usage.write_text(json.dumps({"date": "2000-01-01", "calls": 999}))  # 昨天的计数
    c = _client([_resp(), _resp()], daily_budget=10, usage_path=usage)
    c.complete("p1")
    c.complete("p2")
    data = json.loads(usage.read_text())
    assert data == {"date": date.today().isoformat(), "calls": 2,
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}  # 跨日清零后重新计数


def test_budget_zero_unlimited_and_no_usage_file(tmp_path):
    usage = tmp_path / ".llm_usage.json"
    c = _client([_resp()] * 3, daily_budget=0, usage_path=usage)
    for _ in range(3):
        c.complete("p")
    assert len(c._client.chat.completions.calls) == 3
    assert not usage.exists()  # 不限预算时不写用量文件


def test_corrupted_usage_file_treated_as_zero(tmp_path):
    usage = tmp_path / ".llm_usage.json"
    usage.write_text("{not json")
    c = _client([_resp()], daily_budget=1, usage_path=usage)
    assert c.complete("p") == "ok"  # 损坏文件按 0 计，不阻断调用


def test_token_usage_accumulates(tmp_path):
    """响应带 usage 字段时累计 token；缺字段按 0 计。"""
    usage = tmp_path / ".llm_usage.json"
    c = _client([_resp(usage=(100, 20)), _resp(usage=(50, 10)), _resp()],
                daily_budget=10, usage_path=usage)
    c.complete("p1")
    c.complete("p2")
    c.complete("p3")
    data = json.loads(usage.read_text())
    assert data["calls"] == 3
    assert data["prompt_tokens"] == 150
    assert data["completion_tokens"] == 30
    assert data["total_tokens"] == 180
    assert c.get_usage()["total_tokens"] == 180


def test_usage_recording_thread_safe(tmp_path):
    """并发调用下用量计数不丢失（类级锁保护读写）。"""
    import threading
    usage = tmp_path / ".llm_usage.json"
    c = _client([_resp(usage=(10, 5))] * 20, daily_budget=100, usage_path=usage)
    threads = [threading.Thread(target=c.complete, args=("p",)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(usage.read_text())
    assert data["calls"] == 20
    assert data["total_tokens"] == 300
