#!/usr/bin/env python3
"""
GitLab Duo Proxy — API Key Management
======================================

生成、存储、验证 API 密钥，方便各类客户端接入（如 OpenWebUI、ChatBox 等）。

用法:
    mgr = ApiKeyManager(Path("api_keys.json"))
    await mgr.load()
    key = await mgr.create("my-app")         # 生成返回: "sk-xxxx"
    ok = await mgr.verify("sk-xxxx")         # True/False
    await mgr.report_usage("sk-xxxx")        # 更新使用统计
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("api_keys")


@dataclass
class ApiKey:
    id: str
    name: str
    key_hash: str   # 存储前缀+hash，实际密钥仅在创建时返回一次
    prefix: str     # sk- 前缀 + 前8位
    enabled: bool = True
    request_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    note: str = ""

    def to_dict(self, mask: bool = True) -> Dict:
        d = asdict(self)
        if mask:
            d.pop("key_hash", None)
        return d


class ApiKeyManager:
    """
    线程安全（asyncio.Lock）API 密钥管理器。

    密钥格式: sk-{32位hex随机字符串}
    存储: SHA256 hash (key_hash)，原始密钥仅创建时暴露。
    """

    KEY_PREFIX = "sk-"
    KEY_BYTES = 32

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._keys: Dict[str, ApiKey] = {}     # key_hash -> ApiKey
        self._id_index: Dict[str, str] = {}    # id -> key_hash
        self._lock = asyncio.Lock()

    # ---- Persistence ----
    async def load(self) -> None:
        async with self._lock:
            if self.storage_path.exists():
                try:
                    raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
                    for item in raw.get("keys", []):
                        kh = item["key_hash"]
                        acc = ApiKey(**item)
                        self._keys[kh] = acc
                        self._id_index[acc.id] = kh
                    logger.info("Loaded %d API keys", len(self._keys))
                except Exception as e:
                    logger.error("Failed to load API keys: %s", e)

    async def _save_unlocked(self) -> None:
        data = {
            "keys": [
                {**k.to_dict(mask=False), "key_hash": kh}
                for kh, k in self._keys.items()
            ]
        }
        self.storage_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- Key generation ----
    @staticmethod
    def _raw_key() -> str:
        return ApiKeyManager.KEY_PREFIX + secrets.token_hex(ApiKeyManager.KEY_BYTES)

    @staticmethod
    def _hash_key(raw: str) -> str:
        import hashlib
        return hashlib.sha256(raw.encode()).hexdigest()

    async def create(self, name: str, note: str = "") -> str:
        """
        生成新 API 密钥。返回原始密钥（仅此一次可见），并存入 hash。
        """
        raw = self._raw_key()
        kh = self._hash_key(raw)
        key_id = kh[:12]
        acc = ApiKey(
            id=key_id,
            name=name,
            key_hash=kh,
            prefix=raw[:12] + "...",
            note=note,
        )
        async with self._lock:
            self._keys[kh] = acc
            self._id_index[key_id] = kh
            self._save_unlocked()
        logger.info("API key created: id=%s name=%s", key_id, name)
        return raw

    # ---- Verification ----
    async def verify(self, raw: str) -> Optional[ApiKey]:
        """验证密钥，返回对应的 ApiKey 对象（含使用统计），失败返回 None。"""
        if not raw or not raw.startswith(self.KEY_PREFIX):
            return None
        kh = self._hash_key(raw)
        async with self._lock:
            return self._keys.get(kh)

    async def report_usage(self, raw: str) -> None:
        kh = self._hash_key(raw)
        async with self._lock:
            k = self._keys.get(kh)
            if k and k.enabled:
                k.request_count += 1
                k.last_used_at = time.time()
                # 不每次都写入避免 IO 过多; 关键更新时才 write
                # 这里用轻量策略: 每 10 次写入一次
                if k.request_count % 10 == 0:
                    self._save_unlocked()

    # ---- Management ----
    async def list_all(self) -> List[Dict]:
        async with self._lock:
            return [k.to_dict(mask=True) for k in self._keys.values() if k.enabled]

    async def list_all_full(self) -> List[Dict]:
        """含 disabled 的全部列表。"""
        async with self._lock:
            return [k.to_dict(mask=True) for k in self._keys.values()]

    async def revoke(self, key_id: str) -> bool:
        async with self._lock:
            kh = self._id_index.get(key_id)
            if not kh:
                return False
            k = self._keys.get(kh)
            if k:
                k.enabled = False
                self._save_unlocked()
            return True

    async def rename(self, key_id: str, name: str) -> bool:
        async with self._lock:
            kh = self._id_index.get(key_id)
            if not kh:
                return False
            k = self._keys.get(kh)
            if k:
                k.name = name
                self._save_unlocked()
            return True

    async def get(self, key_id: str) -> Optional[Dict]:
        async with self._lock:
            kh = self._id_index.get(key_id)
            k = self._keys.get(kh) if kh else None
            return k.to_dict(mask=True) if k else None
