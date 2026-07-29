"""反馈收集（Phase 5）：IMAP 轮询发件邮箱，解析用户的回信标注。

邮件卡片上的反馈链接是 mailto：用户点击后生成回信草稿，主题带
    [FB] u=<用户邮箱> p=<论文id> v=<1|2|3|4|5>（五星标注，B2 起替代旧四值；
旧值回信与非法值一样被忽略）。正文可填原因；以 "+" 开头的行是新增检索词
（B4，如 "+CRISPR, 单细胞测序"，逗号兼容中英文），解析后交给
term_expander.add_feedback_terms 追加到该用户自动词表。本模块轮询收件箱中未读的
"[FB]" 回信，解析后写入 feedback 表并标记已读；无法解析的回信
记录日志后同样标记已读（避免毒消息反复重试）。回信主题中的用户标注
可被任何人填写，因此记录前必须校验实际发件人与标注用户一致，防止
伪造回信污染他人学习词表。

IMAP 配置来自 .env：IMAP_HOST 必填（缺失时跳过收集并告警）；
IMAP_PORT 默认 993；IMAP_USER / IMAP_PASSWORD 缺省回退 SMTP_USER / SMTP_PASSWORD。
"""

import email
import email.message
import email.utils
import imaplib
import logging
import os
import re

from dotenv import load_dotenv

from database.db import save_feedback
from processing.term_expander import add_feedback_terms

logger = logging.getLogger(__name__)

SUBJECT_TAG = "[FB]"
VALID_VALUES = {"1", "2", "3", "4", "5"}

_TOKEN = re.compile(r"u=(?P<u>\S+)\s+p=(?P<p>\d+)\s+v=(?P<v>\w+)")


def parse_feedback_message(msg: email.message.Message) -> dict | None:
    """从回信解析 {user_email, paper_id, value, reason}；主题不含合法 token 返回 None。"""
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    if SUBJECT_TAG not in subject:
        return None
    m = _TOKEN.search(subject)
    if not m or m.group("v") not in VALID_VALUES:
        return None
    return {
        "user_email": m.group("u"),
        "paper_id": int(m.group("p")),
        "value": m.group("v"),
        "reason": _plain_body(msg),
    }


def _plain_text(msg: email.message.Message) -> str:
    """取第一个 text/plain 部分并解码为原文（保留行结构）。"""
    part = msg if msg.get_content_type() == "text/plain" else None
    if part is None:
        for p in msg.walk():
            if p.get_content_type() == "text/plain":
                part = p
                break
    if part is None:
        return ""
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _plain_body(msg: email.message.Message) -> str:
    """取正文，去掉引用原信的行，合并剩余非空行（限 500 字符）。"""
    lines = [ln.strip() for ln in _plain_text(msg).splitlines()
             if ln.strip() and not ln.strip().startswith(">")]
    return " ".join(lines)[:500]


def parse_added_terms(msg: email.message.Message) -> list[str]:
    """解析正文中的新增关键词行（B4）：非引用行以 "+" 开头，逗号兼容中英文，
    可多个词（如 "+CRISPR, 单细胞测序"）。这里只做切分与 strip，
    清洗/去重/落盘由 term_expander.add_feedback_terms 负责。"""
    terms = []
    for ln in _plain_text(msg).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(">") or not ln.startswith("+"):
            continue
        terms.extend(part.strip() for part in re.split(r"[,，]", ln[1:]) if part.strip())
    return terms


def collect(conn, imap_factory=imaplib.IMAP4_SSL) -> int:
    """轮询收件箱，把 "[FB]" 回信写入 feedback 表，返回新记录的条数。"""
    load_dotenv()
    host = os.environ.get("IMAP_HOST", "")
    if not host:
        logger.warning("缺少 IMAP_HOST（请在 .env 中填写），跳过反馈收集")
        return 0
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER", "")
    password = os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")

    client = imap_factory(host, port)
    try:
        client.login(user, password)
        client.select("INBOX")
        _, data = client.search(None, f'(UNSEEN SUBJECT "{SUBJECT_TAG}")')
        ids = data[0].split() if data and data[0] else []
        recorded = 0
        for msg_id in ids:
            _, fetched = client.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(fetched[0][1])
            parsed = parse_feedback_message(msg)
            if parsed is None:
                logger.warning("无法解析的反馈回信（msgid %s），标记已读跳过", msg_id.decode())
            else:
                # 防伪造：回信主题中的用户标注可被任何人填写，必须与实际发件人一致
                sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
                if sender != parsed["user_email"].lower():
                    logger.warning("反馈发件人 %s 与标注用户 %s 不符，拒绝记录",
                                   sender or "(空)", parsed["user_email"])
                else:
                    # B4：发件人校验通过后，正文 "+关键词" 行追加到该用户自动词表
                    added_terms = parse_added_terms(msg)
                    if added_terms:
                        add_feedback_terms(sender, added_terms)
                    exists = conn.execute("SELECT 1 FROM papers WHERE id = ?",
                                          (parsed["paper_id"],)).fetchone()
                    if not exists:
                        logger.warning("反馈指向不存在的论文 id=%d，跳过", parsed["paper_id"])
                    elif save_feedback(conn, parsed["user_email"], parsed["paper_id"],
                                       parsed["value"], parsed["reason"]):
                        recorded += 1
            client.store(msg_id, "+FLAGS", "\\Seen")
        return recorded
    finally:
        try:
            client.logout()
        except Exception:
            logger.debug("IMAP logout 失败（连接可能已断开）", exc_info=True)
