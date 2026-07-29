import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest
import yaml

from database.db import connect, save_paper
from feedback.server import create_server
from feedback.tokens import check_token, make_token
from sources.paper import Paper

SECRET = "test-secret"
USER_EMAIL = "a@x.com"


def test_token_roundtrip_and_tamper():
    t = make_token(SECRET, USER_EMAIL, "42", "5")
    assert check_token(SECRET, t, USER_EMAIL, "42", "5")
    # 篡改任一参数或密钥都校验失败
    assert not check_token(SECRET, t, USER_EMAIL, "42", "4")
    assert not check_token(SECRET, t, USER_EMAIL, "43", "5")
    assert not check_token(SECRET, t, "b@x.com", "42", "5")
    assert not check_token("other-secret", t, USER_EMAIL, "42", "5")
    assert not check_token(SECRET, "", USER_EMAIL, "42", "5")


@pytest.fixture
def server(tmp_path):
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    (users_dir / "user001.yaml").write_text(
        yaml.safe_dump({"name": "A", "email": USER_EMAIL}, allow_unicode=True),
        encoding="utf-8")
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    paper = Paper(title="Bee atlas", abstract="abs", authors="Zhang",
                  journal="Nature", date="2026-07-28", doi="10.1/x",
                  url="https://x", keywords=["bee"])
    paper_id = save_paper(conn, paper)
    conn.close()

    srv = create_server(host="127.0.0.1", port=0, db_path=db_path, secret=SECRET,
                        users_dir=users_dir, cache_dir=tmp_path / "auto_terms")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.test_paper_id = paper_id
    srv.test_base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def _get(base, path):
    """返回 (status, body)；HTTP 错误状态也返回响应体而不抛异常。"""
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _fb_path(user, pid, value, token=None):
    token = token if token is not None else make_token(SECRET, user, str(pid), str(value))
    q = urllib.parse.urlencode({"u": user, "p": pid, "v": value, "t": token})
    return f"/fb?{q}"


def test_health(server):
    status, body = _get(server.test_base, "/health")
    assert status == 200 and body == "ok"


def test_fb_valid_rating_recorded(server):
    status, body = _get(server.test_base, _fb_path(USER_EMAIL, server.test_paper_id, 5))
    assert status == 200
    assert "已反馈" in body and "非常重要" in body
    assert "Bee atlas" in body  # 确认页展示论文标题
    conn = connect(server.db_path)
    rows = conn.execute("SELECT user_email, paper_id, value FROM feedback").fetchall()
    conn.close()
    assert [(r["user_email"], r["paper_id"], r["value"]) for r in rows] == [(USER_EMAIL, server.test_paper_id, "5")]


def test_fb_duplicate_rating_not_double_counted(server):
    path = _fb_path(USER_EMAIL, server.test_paper_id, 4)
    assert _get(server.test_base, path)[0] == 200
    status, body = _get(server.test_base, path)
    assert status == 200 and "不会重复计数" in body
    conn = connect(server.db_path)
    n = conn.execute("SELECT COUNT(*) AS c FROM feedback").fetchone()["c"]
    conn.close()
    assert n == 1


def test_fb_rejects_bad_token_and_params(server):
    pid = server.test_paper_id
    # 伪造/篡改 token
    assert _get(server.test_base, _fb_path(USER_EMAIL, pid, 5, token="deadbeef"))[0] == 403
    # 用 v=4 的 token 提交 v=5（篡改星级）
    tampered = _fb_path(USER_EMAIL, pid, 5, token=make_token(SECRET, USER_EMAIL, str(pid), "4"))
    assert _get(server.test_base, tampered)[0] == 403
    # 非法星级与非法论文 id
    assert _get(server.test_base, _fb_path(USER_EMAIL, pid, 9))[0] == 400
    assert _get(server.test_base, _fb_path(USER_EMAIL, "abc", 3))[0] == 400
    # 以上请求都不应落库
    conn = connect(server.db_path)
    n = conn.execute("SELECT COUNT(*) AS c FROM feedback").fetchone()["c"]
    conn.close()
    assert n == 0


def test_kw_form_and_submission(server, tmp_path):
    kw_token = make_token(SECRET, USER_EMAIL, "kw")
    # 无 words：展示表单页（邮件末尾入口）
    status, body = _get(server.test_base, f"/kw?u={urllib.parse.quote(USER_EMAIL)}&t={kw_token}")
    assert status == 200 and "新增关注关键词" in body
    # 提交关键词：写入该用户自动词表的 feedback_added
    words = urllib.parse.quote("单细胞测序,Apis mellifera")
    status, body = _get(server.test_base,
                        f"/kw?u={urllib.parse.quote(USER_EMAIL)}&t={kw_token}&words={words}")
    assert status == 200 and "已加入 2 个关键词" in body
    auto = yaml.safe_load((tmp_path / "auto_terms" / "user001.yaml").read_text(encoding="utf-8"))
    assert auto["feedback_added"] == ["单细胞测序", "Apis mellifera"]


def test_kw_rejects_bad_token(server):
    status, _ = _get(server.test_base, f"/kw?u={urllib.parse.quote(USER_EMAIL)}&t=bad&words=x")
    assert status == 403


def test_create_server_requires_secret(monkeypatch):
    # 防止真实 .env 里的 FEEDBACK_SECRET 干扰：隔离 dotenv 与环境变量
    monkeypatch.setattr("feedback.server.load_dotenv", lambda: None)
    monkeypatch.delenv("FEEDBACK_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="FEEDBACK_SECRET"):
        create_server(port=0, secret="")
