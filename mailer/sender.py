"""SMTP 邮件发送：所有凭据来自 .env，缺失时报错并列出缺失项（禁止硬编码）。"""

import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText

from dotenv import load_dotenv

_REQUIRED = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_FROM_EMAIL"]


def send_email(to_addr: str, subject: str, html_body: str) -> None:
    load_dotenv()
    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"缺少 SMTP 配置：{', '.join(missing)}（请在 .env 中填写）")

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = os.environ["DIGEST_FROM_EMAIL"]
    msg["To"] = to_addr

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    try:
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)
    finally:
        server.quit()
