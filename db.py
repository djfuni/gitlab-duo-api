#!/usr/bin/env python3
"""
GitLab Duo Proxy — Database & Auth (SQLite)
============================================

多用户数据库：
  users     — 用户注册
  accounts  — 每个用户的 GitLab 账号池
  api_keys  — 每个用户的 API 密钥

支持 JWT 认证。
"""

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("db")

# ============================================================
# Database
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role        TEXT DEFAULT 'user',   -- 'admin' | 'user'
    created_at  REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    auth_type   TEXT NOT NULL DEFAULT 'cookie',
    auth_value  TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    status      TEXT DEFAULT 'active',
    cooldown_until REAL DEFAULT 0,
    note        TEXT DEFAULT '',
    created_at  REAL DEFAULT (unixepoch()),
    stats       TEXT DEFAULT '{}'   -- JSON blob
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    key_hash    TEXT UNIQUE NOT NULL,
    prefix      TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    request_count INTEGER DEFAULT 0,
    created_at  REAL DEFAULT (unixepoch()),
    last_used_at REAL DEFAULT 0,
    note        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_apikeys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_apikeys_hash ON api_keys(key_hash);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def execute(self, sql, *params):
        return self._conn.execute(sql, params)

    def executemany(self, sql, seq):
        return self._conn.executemany(sql, seq)

    def fetchone(self, sql, *params):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql, *params):
        return [dict(r) for r in self._conn.execute(sql, params)]

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


# ============================================================
# Auth
# ============================================================

JWT_SECRET = secrets.token_hex(32)

def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return f"{salt}${h.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex() == h
    except Exception:
        return False

def make_jwt(user: dict) -> str:
    """简单自签名 JWT: base64(header).base64(payload).base64(signature)"""
    import base64
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 86400 * 30,  # 30 days
    }).encode()).rstrip(b"=").decode()
    msg = f"{header}.{payload}".encode()
    import hmac
    sig = base64.urlsafe_b64encode(hmac.digest(JWT_SECRET.encode(), msg, "sha256")).rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"

def verify_jwt(token: str) -> Optional[dict]:
    import base64, hmac
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, sig = parts
        # verify signature
        msg = f"{header}.{payload}".encode()
        expected = base64.urlsafe_b64encode(hmac.digest(JWT_SECRET.encode(), msg, "sha256")).rstrip(b"=").decode()
        if not secrets.compare_digest(sig, expected):
            return None
        # decode payload (add padding)
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


# ============================================================
# User & Account Manager
# ============================================================

class DataManager:
    """统一的数据访问层。"""

    def __init__(self, db: Database):
        self.db = db

    # ---- Users ----
    def create_user(self, username: str, password: str, role: str = "user") -> dict:
        uid = secrets.token_hex(10)
        self.db.execute(
            "INSERT INTO users(id, username, password_hash, role) VALUES(?,?,?,?)",
            uid, username, hash_password(password), role
        )
        self.db.commit()
        return self.get_user(uid)

    def get_user(self, uid: str) -> Optional[dict]:
        return self.db.fetchone("SELECT * FROM users WHERE id=?", uid)

    def get_user_by_username(self, username: str) -> Optional[dict]:
        return self.db.fetchone("SELECT * FROM users WHERE username=?", username)

    def login(self, username: str, password: str) -> Optional[str]:
        user = self.get_user_by_username(username)
        if not user or not verify_password(password, user["password_hash"]):
            return None
        return make_jwt(user)

    def verify_token(self, token: str) -> Optional[dict]:
        data = verify_jwt(token)
        if not data:
            return None
        return self.get_user(data["sub"])

    # ---- Accounts ----
    def create_account(self, user_id: str, name: str, auth_type: str,
                       auth_value: str, note: str = "") -> dict:
        aid = secrets.token_hex(10)
        self.db.execute(
            "INSERT INTO accounts(id, user_id, name, auth_type, auth_value, note) VALUES(?,?,?,?,?,?)",
            aid, user_id, name, auth_type, auth_value, note
        )
        self.db.commit()
        return self.get_account(aid)

    def get_account(self, aid: str) -> Optional[dict]:
        row = self.db.fetchone("SELECT * FROM accounts WHERE id=?", aid)
        if row:
            row["stats"] = json.loads(row["stats"]) if row.get("stats") else {}
        return row

    def list_accounts(self, user_id: str) -> List[dict]:
        rows = self.db.fetchall("SELECT * FROM accounts WHERE user_id=? ORDER BY created_at DESC", user_id)
        for r in rows:
            r["stats"] = json.loads(r["stats"]) if r.get("stats") else {}
            r["enabled"] = bool(r["enabled"])
            # mask auth_value
            v = r.get("auth_value", "")
            r["auth_value"] = (v[:8] + "..." + v[-4:]) if len(v) > 16 else ("***" if v else "")
        return rows

    def update_account(self, aid: str, **fields) -> Optional[dict]:
        if not fields:
            return self.get_account(aid)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE accounts SET {sets} WHERE id=?", *fields.values(), aid)
        self.db.commit()
        return self.get_account(aid)

    def delete_account(self, aid: str) -> bool:
        self.db.execute("DELETE FROM accounts WHERE id=?", aid)
        self.db.commit()
        return True

    def get_available_accounts(self, user_id: str) -> List[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM accounts WHERE user_id=? AND enabled=1 AND status='active'",
            user_id
        )
        for r in rows:
            r["stats"] = json.loads(r["stats"]) if r.get("stats") else {}
        return rows

    # ---- API Keys ----
    KEY_PREFIX = "sk-"

    def create_api_key(self, user_id: str, name: str) -> tuple[str, dict]:
        raw = self.KEY_PREFIX + secrets.token_hex(32)
        kh = hashlib.sha256(raw.encode()).hexdigest()
        kid = kh[:12]
        prefix = raw[:14] + "..." + raw[-4:]
        self.db.execute(
            "INSERT INTO api_keys(id, user_id, name, key_hash, prefix) VALUES(?,?,?,?,?)",
            kid, user_id, name, kh, prefix
        )
        self.db.commit()
        return raw, self.get_api_key(kid)

    def verify_api_key(self, raw: str) -> Optional[dict]:
        if not raw or not raw.startswith(self.KEY_PREFIX):
            return None
        kh = hashlib.sha256(raw.encode()).hexdigest()
        key = self.db.fetchone("SELECT * FROM api_keys WHERE key_hash=? AND enabled=1", kh)
        return key

    def report_key_usage(self, raw: str):
        kh = hashlib.sha256(raw.encode()).hexdigest()
        self.db.execute(
            "UPDATE api_keys SET request_count=request_count+1, last_used_at=? WHERE key_hash=?",
            time.time(), kh
        )
        if int(time.time()) % 10 < 2:  # batch commit
            self.db.commit()

    def get_api_key(self, kid: str) -> Optional[dict]:
        return self.db.fetchone("SELECT * FROM api_keys WHERE id=?", kid)

    def list_api_keys(self, user_id: str) -> List[dict]:
        return self.db.fetchall("SELECT * FROM api_keys WHERE user_id=?", user_id)

    def revoke_api_key(self, kid: str) -> bool:
        self.db.execute("UPDATE api_keys SET enabled=0 WHERE id=?", kid)
        self.db.commit()
        return True

    # ---- Config ----
    def get_config(self, key: str, default: str = "") -> str:
        row = self.db.fetchone("SELECT value FROM config WHERE key=?", key)
        return row["value"] if row else default

    def set_config(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", key, value)
        self.db.commit()
