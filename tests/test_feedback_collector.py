"""feedback.collector 的 IMAP 反馈收集测试（用假 IMAP，不碰真实服务器）。"""

from email.message import EmailMessage

import pytest

from database.db import connect, save_paper
from feedback.collector import collect, parse_feedback_message
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
    parsed = parse_feedback_message(_msg("[FB] u=a@x.com p=12 v=save", "因为与我课题相关"))
    assert parsed == {"user_email": "a@x.com", "paper_id": 12,
                      "value": "save", "reason": "因为与我课题相关"}


def test_parse_re_prefix_and_strip_quotes():
    parsed = parse_feedback_message(_msg(
        "Re: [FB] u=a@x.com p=3 v=relevant",
        "我的原因\n> 原邮件引用内容\n> 更多引用",
    ))
    assert parsed["paper_id"] == 3 and parsed["value"] == "relevant"
    assert parsed["reason"] == "我的原因"


def test_parse_invalid():
    assert parse_feedback_message(_msg("普通邮件主题")) is None
    assert parse_feedback_message(_msg("[FB] u=a@x.com p=1 v=喜欢")) is None  # 非法值
    assert parse_feedback_message(_msg("[FB] u=a@x.com p=abc v=save")) is None  # 非数字 id


def test_collect_records_valid_and_marks_all_seen(conn, monkeypatch):
    paper_id = save_paper(conn, Paper(title="t", abstract="a", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    msgs = [
        _msg(f"[FB] u=a@x.com p={paper_id} v=relevant", "不错"),
        _msg("newsletter"),                             # 无标记 → 跳过
        _msg("[FB] u=a@x.com p=99999 v=save"),          # 论文不存在 → 跳过
    ]
    recorded, fake = _run_collect(conn, msgs, monkeypatch)
    assert recorded == 1
    # 所有邮件都标记已读，防止毒消息反复处理
    assert len(fake.seen) == 3
    row = conn.execute("SELECT value FROM feedback WHERE paper_id=?",
                       (paper_id,)).fetchone()
    assert row["value"] == "relevant"


def test_collect_without_imap_host(conn, monkeypatch, caplog):
    monkeypatch.setattr("feedback.collector.load_dotenv", lambda: None)
    monkeypatch.delenv("IMAP_HOST", raising=False)
    with caplog.at_level("WARNING"):
        assert collect(conn) == 0
    assert "IMAP_HOST" in caplog.text


def test_collect_duplicate_ignored(conn, monkeypatch):
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    subject = f"[FB] u=a@x.com p={paper_id} v=save"
    recorded, _ = _run_collect(conn, [_msg(subject), _msg(subject)], monkeypatch)
    assert recorded == 1  # 第二封重复被幂等跳过
