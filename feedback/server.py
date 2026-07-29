"""邮件内五星反馈的 HTTP 接收服务：点星即记录，浏览器直接显示"已反馈"。

日报每张卡片底部嵌入 ⭐1–⭐5 五个链接（digest_builder 生成，带 HMAC token），
指向本服务的 /fb 端点；接收人点一下即写入 feedback 表并返回确认页
（附新增关键词表单，提交到 /kw），全程无需再发邮件。请求无有效 token
一律拒绝（密钥为 .env 的 FEEDBACK_SECRET），防伪造污染他人学习词表。

邮件内链接前缀由 .env 的 FEEDBACK_BASE_URL 决定（接收人设备可访问本机的地址，
如 http://192.168.1.10:8710）；未配置时 digest_builder 回退批量 mailto 回信。

用法：
    python -m feedback.server                  # 0.0.0.0:8710
    python -m feedback.server --port 9000      # 自定义端口
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from database.db import DEFAULT_DB_PATH, connect, save_feedback
from feedback.tokens import check_token, make_token
from processing.term_expander import AUTO_TERMS_DIR, USERS_DIR, add_feedback_terms

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8710

logger = logging.getLogger("feedback.server")

_WORD_SPLIT = re.compile(r"[,，、;；\n]+")
_RATING_LABELS = {
    "1": "完全不相关",
    "2": "不太相关",
    "3": "一般",
    "4": "比较重要",
    "5": "非常重要",
}

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
         background: #f4f5f7; margin: 0; color: #24292f; }}
  .box {{ max-width: 520px; margin: 48px auto; background: #fff; border-radius: 10px;
         padding: 28px 24px; line-height: 1.8; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  .ok {{ color: #1a7f37; }}
  .sub {{ color: #57606a; font-size: 14px; }}
  form {{ margin-top: 18px; border-top: 1px dashed #d0d7de; padding-top: 16px; }}
  input[type=text] {{ width: 100%; box-sizing: border-box; padding: 8px 10px;
                     font-size: 14px; border: 1px solid #d0d7de; border-radius: 6px; }}
  button {{ margin-top: 10px; background: #1f6feb; color: #fff; border: 0;
           border-radius: 6px; padding: 8px 18px; font-size: 14px; }}
</style></head><body><div class="box">{body}</div></body></html>"""


def _render(title: str, body: str) -> str:
    return _PAGE.format(title=escape(title), body=body)


def _keyword_form(secret: str, user_email: str) -> str:
    """确认页/独立页共用的新增关键词表单（token 只绑定用户，不绑定论文）。"""
    t = make_token(secret, user_email, "kw")
    return f"""<form action="/kw" method="get">
<input type="hidden" name="u" value="{escape(user_email)}">
<input type="hidden" name="t" value="{t}">
<div class="sub">有新的关注方向？提交后次日即进入检索：</div>
<input type="text" name="words" placeholder="新增关键词，多个用逗号分隔">
<button type="submit">提交关键词</button>
</form>"""


class FeedbackHandler(BaseHTTPRequestHandler):
    server_version = "LabLitFeedback/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path == "/fb":
            self._handle_fb(q)
        elif parsed.path == "/kw":
            self._handle_kw(q)
        elif parsed.path == "/health":
            self._send(200, "ok", "text/plain; charset=utf-8")
        else:
            self._send(404, _render("页面不存在", '<h1>404</h1><p class="sub">链接有误。</p>'))

    def log_message(self, fmt: str, *args) -> None:  # 访问日志并入 logging
        logger.info("%s %s", self.address_string(), fmt % args)

    # ---- 五星反馈 ----
    def _handle_fb(self, q: dict) -> None:
        u, p, v, t = q.get("u", ""), q.get("p", ""), q.get("v", ""), q.get("t", "")
        if v not in _RATING_LABELS or not p.isdigit():
            return self._send(400, _render("参数错误", "<h1>参数错误</h1><p class=\"sub\">链接不完整。</p>"))
        if not check_token(self.server.secret, t, u, p, v):
            logger.warning("拒绝无效 token 的反馈请求：u=%s p=%s v=%s", u, p, v)
            return self._send(403, _render("链接无效", "<h1>链接无效</h1><p class=\"sub\">校验失败，请使用邮件中的原始链接。</p>"))

        conn = connect(self.server.db_path or DEFAULT_DB_PATH)
        try:
            new = save_feedback(conn, u, int(p), v, reason="web")
            row = conn.execute("SELECT title FROM papers WHERE id = ?", (int(p),)).fetchone()
        finally:
            conn.close()
        title = row["title"] if row else f"#{p}"
        stars = "⭐" * int(v)
        body = (
            f'<h1 class="ok">✓ 已反馈 {stars}（{_RATING_LABELS[v]}）</h1>'
            f'<p class="sub">{escape(title)}</p>'
        )
        if not new:
            body += '<p class="sub">（相同评价此前已记录过，不会重复计数。）</p>'
        body += _keyword_form(self.server.secret, u)
        self._send(200, _render("已反馈", body))

    # ---- 新增关键词 ----
    def _handle_kw(self, q: dict) -> None:
        u, t = q.get("u", ""), q.get("t", "")
        if not u or not check_token(self.server.secret, t, u, "kw"):
            return self._send(403, _render("链接无效", "<h1>链接无效</h1><p class=\"sub\">校验失败，请使用邮件中的原始链接。</p>"))
        words = [w.strip() for w in _WORD_SPLIT.split(q.get("words", "")) if w.strip()]
        if not words:  # 无 words 参数：展示表单页（邮件末尾"新增关键词"入口）
            body = "<h1>新增关注关键词</h1>" + _keyword_form(self.server.secret, u)
            return self._send(200, _render("新增关键词", body))
        added = add_feedback_terms(u, words, users_dir=self.server.users_dir,
                                   cache_dir=self.server.cache_dir)
        if added:
            body = (f'<h1 class="ok">✓ 已加入 {len(added)} 个关键词</h1>'
                    f'<p class="sub">{escape("、".join(added))}<br>次日起参与文献检索与打分。</p>')
        else:
            body = ('<h1>没有新增</h1><p class="sub">关键词为空、无效或已在词表中。</p>')
        self._send(200, _render("关键词已提交", body))

    def _send(self, status: int, payload: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  db_path: Path | None = None, secret: str | None = None,
                  users_dir: Path = USERS_DIR, cache_dir: Path = AUTO_TERMS_DIR) -> ThreadingHTTPServer:
    """组装反馈服务。secret 缺省读 .env 的 FEEDBACK_SECRET，缺失直接拒绝启动。"""
    load_dotenv()
    secret = secret or os.environ.get("FEEDBACK_SECRET", "")
    if not secret:
        raise RuntimeError("缺少 FEEDBACK_SECRET（.env），拒绝启动反馈服务")
    srv = ThreadingHTTPServer((host, port), FeedbackHandler)
    srv.secret = secret
    srv.db_path = db_path  # None → database/db.py 默认 literature_agent.db
    srv.users_dir = Path(users_dir)
    srv.cache_dir = Path(cache_dir)
    return srv


def main() -> int:
    parser = argparse.ArgumentParser(description="邮件内五星反馈 HTTP 接收服务")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "feedback_server.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    srv = create_server(args.host, args.port)
    logger.info("反馈服务已启动：http://%s:%d", args.host, args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
