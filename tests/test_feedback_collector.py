"""feedback.collector 的 IMAP 反馈收集测试（用假 IMAP，不碰真实服务器）。"""

from email.message import EmailMessage

import pytest

from database.db import connect, save_paper
from feedback.collector import collect, parse_added_terms, parse_feedback_message
from sources.paper import Paper


def _msg(subject, body="", sender="a@x.com"):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m.set_content(body)
    return m


class FakeIMAP:
    """最小 IMAP4_SSL 替身：按顺序返回预设邮件，记录已读标记。"""

    def __init__(self, messages):
        self.messages = [m.as_bytes() for m in messages]
        self.seen = []
        self.logged_in = None

    def login(self, user, password):
        self.logged_in = (user, password)

    def select(self, folder):
        assert folder == "INBOX"

    def search(self, charset, criteria):
        assert "UNSEEN" in criteria and "[FB]" in criteria
        return "OK", [b" ".join(str(i).encode() for i in range(len(self.messages)))]

    def fetch(self, msgid, query):
        return "OK", [(None, self.messages[int(msgid)])]

    def store(self, msgid, op, flags):
        assert flags == "\\Seen"
        self.seen.append(msgid)

    def logout(self):
        pass


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _run_collect(conn, messages, monkeypatch):
    """注入假 IMAP 与 IMAP 配置，返回 (新记录数, fake)。"""
    monkeypatch.setattr("feedback.collector.load_dotenv", lambda: None)
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USER", "bot@test")
    monkeypatch.setenv("IMAP_PASSWORD", "pw")
    fake = FakeIMAP(messages)

    def factory(host, port):
        assert host == "imap.test" and port == 993
        return fake

    return collect(conn, imap_factory=factory), fake


def test_parse_valid():
    parsed = parse_feedback_message(_msg("[FB] u=a@x.com p=12 v=5", "因为与我课题相关"))
    assert parsed == {"user_email": "a@x.com", "paper_id": 12,
                      "value": "5", "reason": "因为与我课题相关"}


def test_parse_re_prefix_and_strip_quotes():
    parsed = parse_feedback_message(_msg(
        "Re: [FB] u=a@x.com p=3 v=4",
        "我的原因\n> 原邮件引用内容\n> 更多引用",
    ))
    assert parsed["paper_id"] == 3 and parsed["value"] == "4"
    assert parsed["reason"] == "我的原因"


def test_parse_invalid():
    assert parse_feedback_message(_msg("普通邮件主题")) is None
    assert parse_feedback_message(_msg("[FB] u=a@x.com p=1 v=喜欢")) is None  # 非法值
    assert parse_feedback_message(_msg("[FB] u=a@x.com p=abc v=5")) is None  # 非数字 id
    # 旧四值（B2 前）回信视为非法值忽略
    for old in ("relevant", "not_relevant", "already_read", "save"):
        assert parse_feedback_message(_msg(f"[FB] u=a@x.com p=1 v={old}")) is None


def test_collect_records_valid_and_marks_all_seen(conn, monkeypatch):
    paper_id = save_paper(conn, Paper(title="t", abstract="a", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    msgs = [
        _msg(f"[FB] u=a@x.com p={paper_id} v=5", "不错"),
        _msg("newsletter"),                             # 无标记 → 跳过
        _msg("[FB] u=a@x.com p=99999 v=1"),             # 论文不存在 → 跳过
    ]
    recorded, fake = _run_collect(conn, msgs, monkeypatch)
    assert recorded == 1
    # 所有邮件都标记已读，防止毒消息反复处理
    assert len(fake.seen) == 3
    row = conn.execute("SELECT value FROM feedback WHERE paper_id=?",
                       (paper_id,)).fetchone()
    assert row["value"] == "5"


def test_collect_without_imap_host(conn, monkeypatch, caplog):
    monkeypatch.setattr("feedback.collector.load_dotenv", lambda: None)
    monkeypatch.delenv("IMAP_HOST", raising=False)
    with caplog.at_level("WARNING"):
        assert collect(conn) == 0
    assert "IMAP_HOST" in caplog.text


def test_collect_duplicate_ignored(conn, monkeypatch):
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    subject = f"[FB] u=a@x.com p={paper_id} v=4"
    recorded, _ = _run_collect(conn, [_msg(subject), _msg(subject)], monkeypatch)
    assert recorded == 1  # 第二封重复被幂等跳过


def test_collect_rejects_spoofed_sender(conn, monkeypatch):
    """发件人与主题中标注的用户不符时拒绝记录（防止伪造回信污染他人词表）。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    msgs = [
        # 伪造：attacker 发出的回信标注成受害者 a@x.com
        _msg(f"[FB] u=a@x.com p={paper_id} v=1", sender="attacker@evil.com"),
        # 大小写不敏感：发件人大小写不同仍视为本人
        _msg(f"[FB] u=a@x.com p={paper_id} v=5", sender="A@x.com"),
    ]
    recorded, fake = _run_collect(conn, msgs, monkeypatch)
    assert recorded == 1
    assert len(fake.seen) == 2  # 伪造邮件同样标记已读，不反复重试
    row = conn.execute("SELECT value FROM feedback WHERE paper_id=?", (paper_id,)).fetchone()
    assert row["value"] == "5"  # 只记录了本人那条，伪造的 ⭐1 被丢弃


def test_parse_added_terms():
    """正文 "+" 开头行解析新增关键词：逗号兼容中英文、多个词、引用行不解析。"""
    msg = _msg("[FB] u=a@x.com p=1 v=5",
               "这篇很有帮助\n+CRISPR, 单细胞测序\n+atac-seq，\n> +引用里的词不解析\n")
    assert parse_added_terms(msg) == ["CRISPR", "单细胞测序", "atac-seq"]


def test_parse_added_terms_absent():
    assert parse_added_terms(_msg("[FB] u=a@x.com p=1 v=5", "只是普通原因")) == []


def test_collect_appends_plus_line_terms(conn, monkeypatch):
    """回信正文 "+关键词" 行在发件人校验通过后交给 add_feedback_terms（B4）。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    calls = []
    monkeypatch.setattr("feedback.collector.add_feedback_terms",
                        lambda email, terms: calls.append((email, terms)))
    msgs = [_msg(f"[FB] u=a@x.com p={paper_id} v=4", "+CRISPR, 单细胞测序")]
    recorded, _ = _run_collect(conn, msgs, monkeypatch)
    assert recorded == 1
    assert calls == [("a@x.com", ["CRISPR", "单细胞测序"])]


def test_collect_without_plus_line_behavior_unchanged(conn, monkeypatch):
    """无 "+" 行的普通回信不触发关键词追加，行为完全不变。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    calls = []
    monkeypatch.setattr("feedback.collector.add_feedback_terms",
                        lambda email, terms: calls.append((email, terms)))
    recorded, _ = _run_collect(conn, [_msg(f"[FB] u=a@x.com p={paper_id} v=5", "普通原因")],
                               monkeypatch)
    assert recorded == 1
    assert calls == []


def test_collect_spoofed_sender_terms_rejected(conn, monkeypatch):
    """伪造发件人的 "+关键词" 行不进入自动词表（防伪造校验优先于 B4）。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    calls = []
    monkeypatch.setattr("feedback.collector.add_feedback_terms",
                        lambda email, terms: calls.append((email, terms)))
    msgs = [_msg(f"[FB] u=a@x.com p={paper_id} v=5", "+evilterm",
                 sender="attacker@evil.com")]
    recorded, _ = _run_collect(conn, msgs, monkeypatch)
    assert recorded == 0
    assert calls == []
