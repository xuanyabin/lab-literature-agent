"""反馈链接的 HMAC 签名 token：邮件生成侧（digest_builder）与校验侧（server）共用。

token = HMAC-SHA256(secret, "part1|part2|...") 取前 16 个十六进制字符。
签名覆盖链接的全部关键参数（如 用户邮箱|论文id|星级），篡改任一参数即校验失败；
没有 .env 中的 FEEDBACK_SECRET 无法伪造，防止污染他人学习词表。
"""

import hashlib
import hmac


def make_token(secret: str, *parts: str) -> str:
    """对 parts 生成签名 token（parts 内部不得含 "|"，邮箱/数字 id/星级均满足）。"""
    msg = "|".join(parts).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def check_token(secret: str, token: str, *parts: str) -> bool:
    """常数时间比较 token 是否与 parts 匹配。"""
    return hmac.compare_digest(make_token(secret, *parts), token or "")
