"""反馈收集（Phase 5）：IMAP 轮询发件邮箱，解析用户的回信标注。

支持两种回信格式，主题都带 [FB] tag：
1. 批量反馈（B6 起，日报 Part 3 一键反馈）：`[FB] u=<用户邮箱> d=<日期>`，
   正文按编号标注星级（如 "03: 5"，编号对应该日邮件里的论文顺序，
   经 recommendations 表映射回 paper_id）；
2. 逐篇反馈（日报卡片 ⭐1-5 mailto 链接）：`[FB] u=<用户邮箱> p=<论文id> v=<1-5>`。
正文以 "+" 开头的行是新增检索词（B4，如 "+CRISPR, 单细胞测序"，逗号兼容
中英文），解析后交给 term_expander.add_feedback_terms 追加到该用户自动词表。
本模块轮询收件箱中未读的 "[FB]" 回信，解析后双写——feedback_data/pending
文件队列（学习闭环的数据源，见 feedback/store.py）与 feedback 表（周/月报
get_feedback_since 统计用）——并标记已读；无法解析的回信记录日志后同样标记
已读（避免毒消息反复重试）。回信主题中的用户标注可被任何人填写，因此记录前
必须校验实际发件人与标注用户一致，防止伪造回信污染他人学习词表。

IMAP 配置来自 .env：IMAP_HOST 必填（缺失时跳过收集并告警）；
IMAP_PORT 默认 993；IMAP_USER / IMAP_PASSWORD 缺省回退 SMTP_USER / SMTP_PASSWORD。

另有网页端关键词队列（collect_keyword_queue）：Cloudflare Worker
（worker/feedback.js /kw 端点）把网页版报告里用户直接填写的关键词直写仓库
feedback_data/keywords/pending/（独立目录，不进星标 pending 队列，文件契约见
Worker 文件头注释），本函数解析后同样交给 add_feedback_terms 落地到该用户
自动词表 feedback_added——与邮件 "+关键词" 行完全同一通道，次日检索生效。
"""

import email
import email.message
import email.utils
import imaplib
import logging
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

from database.db import get_recommendation_paper_ids, save_feedback
from feedback import store
from processing.term_expander import AUTO_TERMS_DIR, USERS_DIR, add_feedback_terms

logger = logging.getLogger(__name__)

SUBJECT_TAG = "[FB]"
VALID_VALUES = {"1", "2", "3", "4", "5"}

_TOKEN = re.compile(r"u=(?P<u>\S+)\s+p=(?P<p>\d+)\s+v=(?P<v>\w+)")
_BATCH_TOKEN = re.compile(r"u=(?P<u>\S+)\s+d=(?P<d>\d{4}-\d{2}-\d{2})")
# 批量反馈正文：编号行 "03: 5"（冒号兼容中文/顿号/点，"星"字可带可不带）
_RATING_LINE = re.compile(r"^(\d{1,2})\s*[:：.、]?\s*([1-5])\s*星?\s*$")
# 未填写的编号行（"03:"），不是打分行也不算理由文本
_EMPTY_NUM_LINE = re.compile(r"^\d{1,2}\s*[:：.、]?\s*$")
# 邮件模板自带的说明行，解析理由文本时排除
_TEMPLATE_PREFIXES = ("请直接在编号后填", "如需新增关键词")


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


def parse_batch_feedback_message(msg: email.message.Message) -> dict | None:
    """解析批量反馈回信（B6）：{user_email, date, ratings, reason}；主题不含批量 token 返回 None。

    ratings 为 {编号: 星级字符串}（同一编号重复时以第一行为准）；
    reason 为去掉打分行/空编号行/+关键词行/模板说明行后的剩余正文（限 500 字符）。
    """
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    if SUBJECT_TAG not in subject:
        return None
    m = _BATCH_TOKEN.search(subject)
    if not m:
        return None
    ratings: dict[int, str] = {}
    reason_lines = []
    for ln in _plain_text(msg).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(">"):
            continue
        rating = _RATING_LINE.match(ln)
        if rating:
            ratings.setdefault(int(rating.group(1)), rating.group(2))
            continue
        if _EMPTY_NUM_LINE.match(ln) or ln.startswith("+") or ln.startswith(_TEMPLATE_PREFIXES):
            continue
        reason_lines.append(ln)
    return {
        "user_email": m.group("u"),
        "date": m.group("d"),
        "ratings": ratings,
        "reason": " ".join(reason_lines)[:500],
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


KEYWORD_QUEUE_DIR = store.DEFAULT_BASE_DIR / "keywords"


def collect_keyword_queue(base_dir: Path = KEYWORD_QUEUE_DIR,
                          users_dir: Path = USERS_DIR,
                          cache_dir: Path = AUTO_TERMS_DIR) -> int:
    """消费网页端（Worker /kw）直写的关键词队列 feedback_data/keywords/pending/，
    返回实际新增关键词的文件条数。

    每个文件含 user_email / keyword（逗号兼容中英文，可多个词，切分后与邮件
    "+关键词" 行同语义）/ date / source / timestamp；清洗去重落盘由
    add_feedback_terms 负责。处理后归档 keywords/processed/YYYY-MM/（复用
    store.mark_processed）；损坏文件记日志后留在原地人工排查（同 store.load_pending
    语义）；缺字段或用户不匹配的文件归档跳过（避免毒消息反复重试）。
    """
    pending = Path(base_dir) / "pending"
    if not pending.is_dir():
        return 0
    applied = 0
    for path in sorted(pending.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            logger.warning("关键词反馈文件损坏，跳过：%s", path)
            continue
        user_email = str(data.get("user_email") or "").strip()
        terms = [t.strip() for t in re.split(r"[,，]", str(data.get("keyword") or ""))
                 if t.strip()]
        if not user_email or not terms:
            logger.warning("关键词反馈文件缺 user_email/keyword 字段，归档跳过：%s", path)
        else:
            added = add_feedback_terms(user_email, terms, users_dir, cache_dir)
            if added:
                applied += 1
                logger.info("网页端新增关键词已入词表（%s）：%s", user_email, "、".join(added))
        store.mark_processed(path, base_dir)
    return applied


def collect(conn, imap_factory=imaplib.IMAP4_SSL,
            base_dir: Path = store.DEFAULT_BASE_DIR) -> int:
    """轮询收件箱，把 "[FB]" 回信双写入反馈文件队列与 feedback 表，返回新记录的条数。"""
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
            batch = parse_batch_feedback_message(msg) if parsed is None else None
            if parsed is None and batch is None:
                logger.warning("无法解析的反馈回信（msgid %s），标记已读跳过", msg_id.decode())
            else:
                # 防伪造：回信主题中的用户标注可被任何人填写，必须与实际发件人一致
                claimed = (parsed or batch)["user_email"]
                sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
                if sender != claimed.lower():
                    logger.warning("反馈发件人 %s 与标注用户 %s 不符，拒绝记录",
                                   sender or "(空)", claimed)
                else:
                    # B4：发件人校验通过后，正文 "+关键词" 行追加到该用户自动词表
                    added_terms = parse_added_terms(msg)
                    if added_terms:
                        add_feedback_terms(sender, added_terms)
                    if parsed is not None:
                        exists = conn.execute("SELECT 1 FROM papers WHERE id = ?",
                                              (parsed["paper_id"],)).fetchone()
                        if not exists:
                            logger.warning("反馈指向不存在的论文 id=%d，跳过", parsed["paper_id"])
                        elif _record_one(conn, parsed, base_dir):
                            recorded += 1
                    else:
                        recorded += _record_batch(conn, batch, msg_id, base_dir)
            client.store(msg_id, "+FLAGS", "\\Seen")
        return recorded
    finally:
        try:
            client.logout()
        except Exception:
            logger.debug("IMAP logout 失败（连接可能已断开）", exc_info=True)


def _record_one(conn, entry: dict, base_dir: Path) -> bool:
    """双写一条反馈：pending 文件队列（学习闭环数据源）+ feedback 表（周/月报统计），
    返回是否为新记录（以文件队列为准；db 侧 INSERT OR IGNORE 幂等）。"""
    new = store.save_pending(entry, base_dir) is not None
    save_feedback(conn, entry["user_email"], entry["paper_id"], entry["value"],
                  entry.get("reason", ""))
    return new


def _record_batch(conn, batch: dict, msg_id: bytes, base_dir: Path) -> int:
    """把批量反馈的编号星级映射回论文并双写记录，返回新记录条数。"""
    paper_ids = get_recommendation_paper_ids(conn, batch["user_email"], batch["date"])
    if not paper_ids:
        logger.warning("批量反馈（msgid %s）对应 %s 无推荐记录（用户 %s），跳过",
                       msg_id.decode(), batch["date"], batch["user_email"])
        return 0
    recorded = 0
    for idx, value in batch["ratings"].items():
        if not 1 <= idx <= len(paper_ids):
            logger.warning("批量反馈编号 %02d 超出 %s 推送范围（共 %d 篇），跳过",
                           idx, batch["date"], len(paper_ids))
            continue
        entry = {"user_email": batch["user_email"], "paper_id": paper_ids[idx - 1],
                 "value": value, "reason": batch["reason"], "source": "batch"}
        if _record_one(conn, entry, base_dir):
            recorded += 1
    return recorded
