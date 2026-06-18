#!/usr/bin/env python3
"""Email verification via SMTP (163.com)"""
import logging, random, smtplib, time
from email.mime.text import MIMEText
from typing import Dict

logger = logging.getLogger("email")

SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465
SMTP_USER = "agcwhml2025@163.com"
SMTP_PASS = "SGpCG4bFp7VwKBaA"

_codes: Dict[str, dict] = {}

def send_code(email: str) -> str:
    code = str(random.randint(100000, 999999))
    msg = MIMEText(f"您的 GitLab Duo Proxy 验证码是：{code}\n\n5 分钟内有效。", "plain", "utf-8")
    msg["Subject"] = "GitLab Duo Proxy 邮箱验证"
    msg["From"] = SMTP_USER
    msg["To"] = email
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [email], msg.as_string())
        _codes[email] = {"code": code, "expires": time.time() + 300}
        logger.info("Verification code sent to %s", email)
        return "ok"
    except Exception as e:
        logger.error("SMTP error: %s", e)
        raise

def verify_code(email: str, code: str) -> bool:
    entry = _codes.get(email)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        del _codes[email]
        return False
    if entry["code"] != code:
        return False
    del _codes[email]
    return True
