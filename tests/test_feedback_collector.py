"""feedback.collector 的 IMAP 反馈收集测试（用假 IMAP，不碰真实服务器与 feedback_data/）。"""

import json
from email.message import EmailMessage

import pytest
import yaml

import feedback.collector as collector_mod
import feedback.learner as learner_mod
from database.db import connect, save_paper, save_recommendation
from feedback.collector import (collect, collect_keyword_queue, collect_seed_papers_queue,
                                parse_added_terms, parse_batch_feedback_message,
                                parse_feedback_message, parse_seed_paper_lines)
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


@pytest.fixture
def store_dir(tmp_path):
    """隔离的反馈文件队列目录（不碰仓库真实的 feedback_data/）。"""
    return tmp_path / "feedback_data"


def _pending_files(store_dir):
    return sorted((store_dir / "pending").glob("*.yaml"))


def _run_collect(conn, messages, monkeypatch, base_dir):
    """注入假 IMAP 与 IMAP 配置，返回 (新记录数, fake)。"""
    monkeypatch.setattr("feedback.collector.load_dotenv", lambda: None)
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USER", "bot@test")
    monkeypatch.setenv("IMAP_PASSWORD", "pw")
    fake = FakeIMAP(messages)

    def factory(host, port):
        assert host == "imap.test" and port == 993
        return fake

    return collect(conn, imap_factory=factory, base_dir=base_dir), fake


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


def test_collect_records_valid_and_marks_all_seen(conn, monkeypatch, store_dir):
    paper_id = save_paper(conn, Paper(title="t", abstract="a", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    msgs = [
        _msg(f"[FB] u=a@x.com p={paper_id} v=5", "不错"),
        _msg("newsletter"),                             # 无标记 → 跳过
        _msg("[FB] u=a@x.com p=99999 v=1"),             # 论文不存在 → 跳过
    ]
    recorded, fake = _run_collect(conn, msgs, monkeypatch, store_dir)
    assert recorded == 1
    # 所有邮件都标记已读，防止毒消息反复处理
    assert len(fake.seen) == 3
    row = conn.execute("SELECT value FROM feedback WHERE paper_id=?",
                       (paper_id,)).fetchone()
    assert row["value"] == "5"


def test_collect_double_writes_pending_file(conn, monkeypatch, store_dir):
    """逐篇 [FB] 回信双写：pending 文件队列（学习用）+ feedback 表（统计用）。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="a", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    msgs = [_msg(f"[FB] u=a@x.com p={paper_id} v=4", "与课题相关")]
    recorded, _ = _run_collect(conn, msgs, monkeypatch, store_dir)
    assert recorded == 1
    files = _pending_files(store_dir)
    assert len(files) == 1
    data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert data["user_email"] == "a@x.com"
    assert data["paper_id"] == paper_id
    assert data["value"] == "4"
    assert data["reason"] == "与课题相关"
    # db 侧同步写入（周/月报 get_feedback_since 统计用）
    row = conn.execute("SELECT value FROM feedback WHERE paper_id=?",
                       (paper_id,)).fetchone()
    assert row["value"] == "4"


def test_collect_without_imap_host(conn, monkeypatch, caplog):
    monkeypatch.setattr("feedback.collector.load_dotenv", lambda: None)
    monkeypatch.delenv("IMAP_HOST", raising=False)
    with caplog.at_level("WARNING"):
        assert collect(conn) == 0
    assert "IMAP_HOST" in caplog.text


def test_collect_duplicate_ignored(conn, monkeypatch, store_dir):
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    subject = f"[FB] u=a@x.com p={paper_id} v=4"
    recorded, _ = _run_collect(conn, [_msg(subject), _msg(subject)], monkeypatch, store_dir)
    assert recorded == 1  # 第二封重复被幂等跳过
    assert len(_pending_files(store_dir)) == 1  # 文件队列同样幂等


def test_collect_rejects_spoofed_sender(conn, monkeypatch, store_dir):
    """发件人与主题中标注的用户不符时拒绝记录（防止伪造回信污染他人词表）。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    msgs = [
        # 伪造：attacker 发出的回信标注成受害者 a@x.com
        _msg(f"[FB] u=a@x.com p={paper_id} v=1", sender="attacker@evil.com"),
        # 大小写不敏感：发件人大小写不同仍视为本人
        _msg(f"[FB] u=a@x.com p={paper_id} v=5", sender="A@x.com"),
    ]
    recorded, fake = _run_collect(conn, msgs, monkeypatch, store_dir)
    assert recorded == 1
    assert len(fake.seen) == 2  # 伪造邮件同样标记已读，不反复重试
    row = conn.execute("SELECT value FROM feedback WHERE paper_id=?", (paper_id,)).fetchone()
    assert row["value"] == "5"  # 只记录了本人那条，伪造的 ⭐1 被丢弃
    assert len(_pending_files(store_dir)) == 1  # 伪造反馈不进文件队列


def test_parse_added_terms():
    """正文 "+" 开头行解析新增关键词：逗号兼容中英文、多个词、引用行不解析。"""
    msg = _msg("[FB] u=a@x.com p=1 v=5",
               "这篇很有帮助\n+CRISPR, 单细胞测序\n+atac-seq，\n> +引用里的词不解析\n")
    assert parse_added_terms(msg) == ["CRISPR", "单细胞测序", "atac-seq"]


def test_parse_added_terms_absent():
    assert parse_added_terms(_msg("[FB] u=a@x.com p=1 v=5", "只是普通原因")) == []


def test_collect_appends_plus_line_terms(conn, monkeypatch, store_dir):
    """回信正文 "+关键词" 行在发件人校验通过后交给 add_feedback_terms（B4）。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    calls = []
    monkeypatch.setattr("feedback.collector.add_feedback_terms",
                        lambda email, terms: calls.append((email, terms)))
    msgs = [_msg(f"[FB] u=a@x.com p={paper_id} v=4", "+CRISPR, 单细胞测序")]
    recorded, _ = _run_collect(conn, msgs, monkeypatch, store_dir)
    assert recorded == 1
    assert calls == [("a@x.com", ["CRISPR", "单细胞测序"])]


def test_collect_without_plus_line_behavior_unchanged(conn, monkeypatch, store_dir):
    """无 "+" 行的普通回信不触发关键词追加，行为完全不变。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    calls = []
    monkeypatch.setattr("feedback.collector.add_feedback_terms",
                        lambda email, terms: calls.append((email, terms)))
    recorded, _ = _run_collect(conn, [_msg(f"[FB] u=a@x.com p={paper_id} v=5", "普通原因")],
                               monkeypatch, store_dir)
    assert recorded == 1
    assert calls == []


def test_collect_spoofed_sender_terms_rejected(conn, monkeypatch, store_dir):
    """伪造发件人的 "+关键词" 行不进入自动词表（防伪造校验优先于 B4）。"""
    paper_id = save_paper(conn, Paper(title="t", abstract="", authors="", journal="",
                                      date="2026-07-22", doi="", url=""))
    calls = []
    monkeypatch.setattr("feedback.collector.add_feedback_terms",
                        lambda email, terms: calls.append((email, terms)))
    msgs = [_msg(f"[FB] u=a@x.com p={paper_id} v=5", "+evilterm",
                 sender="attacker@evil.com")]
    recorded, _ = _run_collect(conn, msgs, monkeypatch, store_dir)
    assert recorded == 0
    assert calls == []
    assert _pending_files(store_dir) == []  # 伪造反馈也不进文件队列


# ---------- 批量反馈（B6：一封邮件按编号标注星级） ----------

def _save_recs(conn, user_email, sent_date, pid_scores):
    """按 (paper_id, score) 写入推荐记录，模拟当日推送。"""
    for pid, score in pid_scores:
        save_recommendation(conn, user_email, pid, "Reference", score, sent_date)


def _save_papers(conn, n):
    return [save_paper(conn, Paper(title=f"t{i}", abstract="a", authors="", journal="",
                                   date="2026-07-22", doi=f"10.1/{i}", url=""))
            for i in range(n)]


def test_parse_batch_valid():
    msg = _msg("[FB] u=a@x.com d=2026-07-28",
               "请直接在编号后填写 1-5 星评分（只填想评的编号，其余留空）：\n"
               "01: 5\n02: \n03: 4星\n整体不错\n"
               "如需新增关键词，请在下方 + 号后填写（每行一个，可用逗号分隔）：\n+\n")
    parsed = parse_batch_feedback_message(msg)
    assert parsed["user_email"] == "a@x.com" and parsed["date"] == "2026-07-28"
    assert parsed["ratings"] == {1: "5", 3: "4"}  # 空编号行不解析
    assert parsed["reason"] == "整体不错"  # 模板说明行/打分行/+行不进理由


def test_parse_batch_invalid():
    assert parse_batch_feedback_message(_msg("普通邮件主题")) is None
    # 旧逐篇格式不匹配批量 token（由 parse_feedback_message 处理）
    assert parse_batch_feedback_message(_msg("[FB] u=a@x.com p=1 v=5")) is None
    assert parse_batch_feedback_message(_msg("[FB] u=a@x.com d=昨天")) is None


def test_collect_batch_maps_numbers_to_papers(conn, monkeypatch, store_dir):
    ids = _save_papers(conn, 3)
    # 展示顺序按分数降序：ids[2]=01 号、ids[0]=02 号、ids[1]=03 号
    _save_recs(conn, "a@x.com", "2026-07-28",
               [(ids[0], 60), (ids[1], 50), (ids[2], 70)])
    msg = _msg("[FB] u=a@x.com d=2026-07-28", "01: 5\n03: 2\n")
    recorded, fake = _run_collect(conn, [msg], monkeypatch, store_dir)
    assert recorded == 2
    assert len(fake.seen) == 1
    rows = {r["paper_id"]: r["value"]
            for r in conn.execute("SELECT paper_id, value FROM feedback").fetchall()}
    assert rows == {ids[2]: "5", ids[1]: "2"}
    # 每颗星各写一个 pending 文件（学习队列以文件为准）
    assert len(_pending_files(store_dir)) == 2


def test_collect_batch_out_of_range_skipped(conn, monkeypatch, caplog, store_dir):
    ids = _save_papers(conn, 1)
    _save_recs(conn, "a@x.com", "2026-07-28", [(ids[0], 60)])
    msg = _msg("[FB] u=a@x.com d=2026-07-28", "01: 5\n09: 3\n")
    with caplog.at_level("WARNING"):
        recorded, _ = _run_collect(conn, [msg], monkeypatch, store_dir)
    assert recorded == 1  # 越界编号 09 跳过，合法编号 01 正常记录
    assert "超出" in caplog.text


def test_collect_batch_no_recommendations(conn, monkeypatch, caplog, store_dir):
    msg = _msg("[FB] u=a@x.com d=2026-07-28", "01: 5\n")
    with caplog.at_level("WARNING"):
        recorded, fake = _run_collect(conn, [msg], monkeypatch, store_dir)
    assert recorded == 0
    assert "无推荐记录" in caplog.text
    assert len(fake.seen) == 1  # 无法映射也标记已读，不反复重试


def test_collect_batch_rejects_spoofed_sender(conn, monkeypatch, store_dir):
    ids = _save_papers(conn, 1)
    _save_recs(conn, "a@x.com", "2026-07-28", [(ids[0], 60)])
    msg = _msg("[FB] u=a@x.com d=2026-07-28", "01: 1\n",
               sender="attacker@evil.com")
    recorded, _ = _run_collect(conn, [msg], monkeypatch, store_dir)
    assert recorded == 0
    assert conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"] == 0
    assert _pending_files(store_dir) == []


def test_collect_batch_plus_terms_still_work(conn, monkeypatch, store_dir):
    """批量回信中的 "+关键词" 行同样追加到自动词表（B4 与 B6 共存）。"""
    ids = _save_papers(conn, 1)
    _save_recs(conn, "a@x.com", "2026-07-28", [(ids[0], 60)])
    calls = []
    monkeypatch.setattr("feedback.collector.add_feedback_terms",
                        lambda email, terms: calls.append((email, terms)))
    msg = _msg("[FB] u=a@x.com d=2026-07-28", "01: 4\n+CRISPR\n+\n")
    recorded, _ = _run_collect(conn, [msg], monkeypatch, store_dir)
    assert recorded == 1
    assert calls == [("a@x.com", ["CRISPR"])]  # 单独的 "+" 解析为空，不产生词


# ---------- 网页端关键词队列（Worker /kw 直写 feedback_data/keywords/pending/） ----------


def _kw_dir(tmp_path):
    """隔离的网页端关键词队列目录 + 用户目录/自动词表缓存目录。"""
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    (users_dir / "user001.yaml").write_text("email: a@x.com\n", encoding="utf-8")
    return tmp_path / "feedback_data" / "keywords", users_dir, tmp_path / "auto_terms"


def _write_kw(base_dir, name, record=None, raw=None):
    pending = base_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else yaml.safe_dump(record, allow_unicode=True)
    (pending / name).write_text(text, encoding="utf-8")


def test_keyword_queue_applies_terms_and_archives(tmp_path):
    """正常关键词文件：切分（逗号兼容中英文）→ 清洗入自动词表 feedback_added → 归档。"""
    base_dir, users_dir, cache_dir = _kw_dir(tmp_path)
    _write_kw(base_dir, "kw_u00000000_abc.yaml", {
        "user_email": "a@x.com", "keyword": "CRISPR, 单细胞测序",
        "date": "2026-08-04", "source": "keyword_webhook",
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    applied = collect_keyword_queue(base_dir, users_dir, cache_dir)
    assert applied == 1
    auto = yaml.safe_load((cache_dir / "user001.yaml").read_text(encoding="utf-8"))
    assert auto["feedback_added"] == ["CRISPR", "单细胞测序"]
    assert list((base_dir / "pending").glob("*.yaml")) == []
    assert [p.name for p in (base_dir / "processed" / "2026-08").glob("*.yaml")] == \
        ["kw_u00000000_abc.yaml"]


def test_keyword_queue_corrupt_file_stays(tmp_path):
    """损坏文件记日志后留在 pending 原地（同 store.load_pending 语义），不阻塞后续文件。"""
    base_dir, users_dir, cache_dir = _kw_dir(tmp_path)
    _write_kw(base_dir, "kw_bad.yaml", raw="{ not: valid: yaml: [")
    _write_kw(base_dir, "kw_ok.yaml", {
        "user_email": "a@x.com", "keyword": "atac-seq", "date": "2026-08-04",
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    applied = collect_keyword_queue(base_dir, users_dir, cache_dir)
    assert applied == 1
    assert [p.name for p in (base_dir / "pending").glob("*.yaml")] == ["kw_bad.yaml"]


def test_keyword_queue_missing_fields_archived(tmp_path):
    """缺 user_email/keyword 的文件归档跳过（避免毒消息反复重试），不入词表。"""
    base_dir, users_dir, cache_dir = _kw_dir(tmp_path)
    _write_kw(base_dir, "kw_empty.yaml", {"user_email": "a@x.com", "keyword": ""})
    applied = collect_keyword_queue(base_dir, users_dir, cache_dir)
    assert applied == 0
    assert list((base_dir / "pending").glob("*.yaml")) == []
    assert not (cache_dir / "user001.yaml").exists()


def test_keyword_queue_unknown_user_archived(tmp_path, caplog):
    """email 不匹配任何用户 yaml 时归档跳过（add_feedback_terms 自身记 warning）。"""
    base_dir, users_dir, cache_dir = _kw_dir(tmp_path)
    _write_kw(base_dir, "kw_unknown.yaml", {
        "user_email": "ghost@x.com", "keyword": "CRISPR",
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    applied = collect_keyword_queue(base_dir, users_dir, cache_dir)
    assert applied == 0
    assert list((base_dir / "pending").glob("*.yaml")) == []


def test_keyword_queue_no_pending_dir(tmp_path):
    """队列目录不存在（从未有网页端提交）时静默返回 0。"""
    base_dir, users_dir, cache_dir = _kw_dir(tmp_path)
    assert collect_keyword_queue(base_dir, users_dir, cache_dir) == 0


# ---------- 网页端"用文献优化关键词"队列（Worker /sp 直写 seed_papers/pending/） ----------


def _sp_setup(tmp_path):
    """隔离的文献输入队列目录 + 用户目录/自动词表缓存目录 + 内存 db。"""
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    (users_dir / "user001.yaml").write_text("email: a@x.com\n", encoding="utf-8")
    conn = connect(tmp_path / "test.db")
    return tmp_path / "feedback_data" / "seed_papers", users_dir, \
        tmp_path / "auto_terms", conn


def _write_sp(base_dir, name, record=None, raw=None):
    pending = base_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else yaml.safe_dump(record, allow_unicode=True)
    (pending / name).write_text(text, encoding="utf-8")


class _TermLLM:
    """extract_terms 走真实 JSON 解析：每次调用返回固定两个新词。"""

    def __init__(self, terms=("spatial transcriptomics", "cell segmentation")):
        self.terms = terms

    def complete(self, prompt):
        return json.dumps(list(self.terms))


def _fake_fetch(pmids):
    return [Paper(title=f"Paper {pmids[0]}", abstract="abs", authors="Au",
                  journal="J", date="2026-08-01", doi="", url="http://x",
                  keywords=[])]


def test_parse_seed_paper_lines_valid_invalid_and_cap():
    valid, invalid = parse_seed_paper_lines(
        "12345678\n10.1038/s41586-023-0001\n\nnot a doi\n10.1/no space allowed x\n")
    assert valid == [("pmid", "12345678"), ("doi", "10.1038/s41586-023-0001")]
    assert invalid == ["not a doi", "10.1/no space allowed x"]
    many = "\n".join(str(10000000 + i) for i in range(15))
    valid, _ = parse_seed_paper_lines(many)
    assert len(valid) == 10  # 单次提交上限 10 篇


def test_seed_papers_queue_extracts_terms_and_archives(tmp_path, monkeypatch):
    """2 篇 PMID + 1 非法行：PMID 提词落 feedback_added、非法行跳过、文件归档、审计记录来源。"""
    base_dir, users_dir, cache_dir, conn = _sp_setup(tmp_path)
    audit = tmp_path / "audit.log"
    monkeypatch.setattr(learner_mod, "AUDIT_LOG", audit)
    monkeypatch.setattr(collector_mod, "fetch_by_pmids", _fake_fetch)
    monkeypatch.setattr(collector_mod, "pmid_for_doi",
                        lambda doi: "999" if doi == "10.1/x" else None)
    _write_sp(base_dir, "sp_u00000000_abc.yaml", {
        "user_email": "a@x.com", "papers": "12345678\n87654321\nnot-a-doi",
        "date": "2026-08-04", "source": "seed_papers_webhook",
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    users = [{"email": "a@x.com"}]
    applied = collect_seed_papers_queue(users, conn, _TermLLM(), base_dir,
                                        users_dir, cache_dir)
    assert applied == 1
    auto = yaml.safe_load((cache_dir / "user001.yaml").read_text(encoding="utf-8"))
    assert auto["feedback_added"] == ["spatial transcriptomics", "cell segmentation"]
    assert list((base_dir / "pending").glob("*.yaml")) == []
    assert [p.name for p in (base_dir / "processed" / "2026-08").glob("*.yaml")] == \
        ["sp_u00000000_abc.yaml"]
    records = [json.loads(ln) for ln in audit.read_text(encoding="utf-8").splitlines()]
    assert {r["action"] for r in records} == {"seed_term"}
    assert {r["source_ref"] for r in records} == {"pmid:12345678"}
    conn.close()


def test_seed_papers_queue_doi_not_found_skipped(tmp_path, monkeypatch, caplog):
    """DOI 转 PMID 失败：跳过该篇记日志，不影响同批其余文献，文件仍归档。"""
    base_dir, users_dir, cache_dir, conn = _sp_setup(tmp_path)
    audit = tmp_path / "audit.log"
    monkeypatch.setattr(learner_mod, "AUDIT_LOG", audit)
    monkeypatch.setattr(collector_mod, "fetch_by_pmids", _fake_fetch)
    monkeypatch.setattr(collector_mod, "pmid_for_doi", lambda doi: None)
    _write_sp(base_dir, "sp_a.yaml", {
        "user_email": "a@x.com", "papers": "10.1/unknown\n12345678",
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    applied = collect_seed_papers_queue([{"email": "a@x.com"}], conn, _TermLLM(),
                                        base_dir, users_dir, cache_dir)
    assert applied == 1  # PMID 那篇仍提炼成功
    auto = yaml.safe_load((cache_dir / "user001.yaml").read_text(encoding="utf-8"))
    assert auto["feedback_added"] == ["spatial transcriptomics", "cell segmentation"]
    assert list((base_dir / "pending").glob("*.yaml")) == []
    conn.close()


def test_seed_papers_queue_terms_capped_at_20(tmp_path, monkeypatch):
    """单次提交总量封顶 20 词（每篇 ≤5 词由 extract_terms 保证，这里 10 篇 × 3 词 = 30 → 20）。"""
    base_dir, users_dir, cache_dir, conn = _sp_setup(tmp_path)
    audit = tmp_path / "audit.log"
    monkeypatch.setattr(learner_mod, "AUDIT_LOG", audit)
    monkeypatch.setattr(collector_mod, "fetch_by_pmids", _fake_fetch)
    papers = "\n".join(str(10000000 + i) for i in range(10))

    class _CountingLLM:
        """每次调用返回 3 个新词，避免被去重逻辑吞掉。"""
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            n = self.calls
            return json.dumps([f"term {n}a", f"term {n}b", f"term {n}c"])

    llm = _CountingLLM()
    _write_sp(base_dir, "sp_cap.yaml", {
        "user_email": "a@x.com", "papers": papers,
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    applied = collect_seed_papers_queue([{"email": "a@x.com"}], conn, llm,
                                        base_dir, users_dir, cache_dir)
    assert applied == 1
    auto = yaml.safe_load((cache_dir / "user001.yaml").read_text(encoding="utf-8"))
    assert len(auto["feedback_added"]) == 20
    conn.close()


def test_seed_papers_queue_all_failed_warns_but_archives(tmp_path, monkeypatch, caplog):
    """全部文献抓取失败：只告警不抛异常，文件归档，不写词表。"""
    base_dir, users_dir, cache_dir, conn = _sp_setup(tmp_path)
    monkeypatch.setattr(learner_mod, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(collector_mod, "fetch_by_pmids", lambda pmids: [])
    _write_sp(base_dir, "sp_fail.yaml", {
        "user_email": "a@x.com", "papers": "12345678",
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    with caplog.at_level("WARNING"):
        applied = collect_seed_papers_queue([{"email": "a@x.com"}], conn,
                                            _TermLLM(), base_dir, users_dir, cache_dir)
    assert applied == 0
    assert not (cache_dir / "user001.yaml").exists()
    assert list((base_dir / "pending").glob("*.yaml")) == []
    assert "未提炼出任何新检索词" in caplog.text
    conn.close()


def test_seed_papers_queue_corrupt_and_unknown_user(tmp_path, monkeypatch):
    """损坏文件留原地；用户不匹配的归档跳过。"""
    base_dir, users_dir, cache_dir, conn = _sp_setup(tmp_path)
    monkeypatch.setattr(learner_mod, "AUDIT_LOG", tmp_path / "audit.log")
    _write_sp(base_dir, "sp_bad.yaml", raw="{ not: valid: yaml: [")
    _write_sp(base_dir, "sp_unknown.yaml", {
        "user_email": "ghost@x.com", "papers": "12345678",
        "timestamp": "2026-08-04T01:02:03+00:00",
    })
    applied = collect_seed_papers_queue([{"email": "a@x.com"}], conn, _TermLLM(),
                                        base_dir, users_dir, cache_dir)
    assert applied == 0
    assert [p.name for p in (base_dir / "pending").glob("*.yaml")] == ["sp_bad.yaml"]
    conn.close()


def test_seed_papers_queue_no_pending_dir(tmp_path):
    base_dir, users_dir, cache_dir, conn = _sp_setup(tmp_path)
    assert collect_seed_papers_queue([{"email": "a@x.com"}], conn, _TermLLM(),
                                     base_dir, users_dir, cache_dir) == 0
    conn.close()
