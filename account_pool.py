#!/usr/bin/env python3
"""
GitLab Duo Chat Proxy — Account Pool
====================================

账号池模块：管理多个 GitLab 账号，提供轮询/随机/最少使用调度策略，
自动失败冷却与重试切换，调用统计，持久化到 accounts.json。

用法:
    pool = AccountPool(Path("accounts.json"))
    await pool.load()
    account = await pool.acquire()          # 取一个可用账号
    await pool.report_success(account.id)   # 上报成功
    await pool.report_failure(account.id, "401 unauthorized")  # 上报失败
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("account_pool")


# ============================================================
# Data Models
# ============================================================

@dataclass
class AccountStats:
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_used_at: float = 0.0       # unix ts
    last_success_at: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0


@dataclass
class Account:
    id: str
    name: str
    auth_type: str                  # cookie | token | session | oauth
    auth_value: str
    enabled: bool = True
    status: str = "active"          # active | cooldown | disabled | invalid
    cooldown_until: float = 0.0     # unix ts
    note: str = ""
    created_at: float = field(default_factory=time.time)
    stats: AccountStats = field(default_factory=AccountStats)

    # in-flight request counter (runtime only, not persisted)
    in_flight: int = field(default=0, repr=False)

    def to_dict(self, mask: bool = True) -> Dict:
        d = asdict(self)
        # remove runtime-only fields
        d.pop("in_flight", None)
        if mask and d.get("auth_value"):
            v = d["auth_value"]
            if len(v) > 16:
                d["auth_value"] = v[:8] + "..." + v[-4:]
            else:
                d["auth_value"] = "***"
        return d

    def is_available(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.status == "disabled" or self.status == "invalid":
            return False
        if self.status == "cooldown" and now < self.cooldown_until:
            return False
        # cooldown expired → reactivate
        if self.status == "cooldown" and now >= self.cooldown_until:
            return True
        return True


# ============================================================
# Scheduler
# ============================================================

SCHEDULE_STRATEGIES = ("round_robin", "random", "least_used")


class AccountPool:
    """
    Async-safe account pool with scheduling, cooldown and stats.
    """

    def __init__(
        self,
        storage_path: Path,
        strategy: str = "round_robin",
        cooldown_seconds: int = 60,
        max_consecutive_failures: int = 3,
        invalid_on_auth_error: bool = True,
    ):
        self.storage_path = storage_path
        self.strategy = strategy if strategy in SCHEDULE_STRATEGIES else "round_robin"
        self.cooldown_seconds = cooldown_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.invalid_on_auth_error = invalid_on_auth_error

        self._accounts: Dict[str, Account] = {}
        self._rr_index = 0
        self._lock = asyncio.Lock()
        self._loaded = False

    # ---------------- Persistence ----------------

    async def load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self.storage_path.exists():
                try:
                    raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
                    for item in raw.get("accounts", []):
                        stats_data = item.pop("stats", {}) or {}
                        acc = Account(**item)
                        acc.stats = AccountStats(**stats_data)
                        acc.in_flight = 0
                        # reset transient status
                        if acc.status == "cooldown":
                            acc.status = "active"
                            acc.cooldown_until = 0.0
                        self._accounts[acc.id] = acc
                    logger.info("Loaded %d accounts from %s", len(self._accounts), self.storage_path)
                except Exception as e:
                    logger.error("Failed to load accounts: %s", e)
            self._loaded = True

    async def save(self) -> None:
        async with self._lock:
            self._write_unlocked()

    def _write_unlocked(self) -> None:
        data = {
            "strategy": self.strategy,
            "cooldown_seconds": self.cooldown_seconds,
            "max_consecutive_failures": self.max_consecutive_failures,
            "accounts": [
                {**a.to_dict(mask=False), "stats": asdict(a.stats)}
                for a in self._accounts.values()
            ],
        }
        try:
            self.storage_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("Failed to save accounts: %s", e)

    # ---------------- CRUD ----------------

    async def add(self, name: str, auth_type: str, auth_value: str,
                  note: str = "", enabled: bool = True) -> Account:
        async with self._lock:
            acc = Account(
                id=uuid.uuid4().hex[:12],
                name=name,
                auth_type=auth_type,
                auth_value=auth_value,
                note=note,
                enabled=enabled,
            )
            self._accounts[acc.id] = acc
            self._write_unlocked()
            return acc

    async def update(self, account_id: str, **fields) -> Optional[Account]:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return None
            for k in ("name", "auth_type", "auth_value", "note", "enabled"):
                if k in fields and fields[k] is not None:
                    setattr(acc, k, fields[k])
            # re-validate after value change
            if "auth_value" in fields and fields["auth_value"]:
                if acc.status == "invalid":
                    acc.status = "active"
                acc.stats.consecutive_failures = 0
            self._write_unlocked()
            return acc

    async def delete(self, account_id: str) -> bool:
        async with self._lock:
            if account_id in self._accounts:
                del self._accounts[account_id]
                self._write_unlocked()
                return True
            return False

    async def get(self, account_id: str) -> Optional[Account]:
        async with self._lock:
            return self._accounts.get(account_id)

    async def list_all(self, mask: bool = True) -> List[Dict]:
        async with self._lock:
            return [a.to_dict(mask=mask) for a in self._accounts.values()]

    async def set_enabled(self, account_id: str, enabled: bool) -> Optional[Account]:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return None
            acc.enabled = enabled
            if enabled and acc.status == "disabled":
                acc.status = "active"
            elif not enabled:
                acc.status = "disabled"
            self._write_unlocked()
            return acc

    async def reset_status(self, account_id: str) -> Optional[Account]:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return None
            acc.status = "active"
            acc.cooldown_until = 0.0
            acc.stats.consecutive_failures = 0
            acc.enabled = True
            self._write_unlocked()
            return acc

    # ---------------- Scheduling ----------------

    async def acquire(self, exclude: Optional[List[str]] = None) -> Optional[Account]:
        """
        Pick the next available account per strategy.
        Returns None if no account is available.
        """
        exclude = exclude or []
        async with self._lock:
            now = time.time()
            # reactivate expired cooldowns
            for acc in self._accounts.values():
                if acc.status == "cooldown" and now >= acc.cooldown_until:
                    acc.status = "active"
                    acc.cooldown_until = 0.0

            candidates = [
                a for a in self._accounts.values()
                if a.id not in exclude and a.is_available(now)
            ]
            if not candidates:
                return None

            if self.strategy == "random":
                import random
                return random.choice(candidates)

            if self.strategy == "least_used":
                # prefer least in-flight, then least total requests
                candidates.sort(key=lambda a: (a.in_flight, a.stats.total_requests))
                chosen = candidates[0]
            else:
                # round robin by order in dict
                order = list(self._accounts.values())
                n = len(order)
                chosen = None
                for i in range(n):
                    idx = (self._rr_index + i) % n
                    cand = order[idx]
                    if cand in candidates:
                        chosen = cand
                        self._rr_index = (idx + 1) % n
                        break
                if chosen is None:
                    chosen = candidates[0]

            chosen.in_flight += 1
            chosen.stats.total_requests += 1
            chosen.stats.last_used_at = now
            return chosen

    async def release(self, account_id: str) -> None:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if acc and acc.in_flight > 0:
                acc.in_flight -= 1

    async def report_success(self, account_id: str) -> None:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return
            now = time.time()
            acc.stats.success_count += 1
            acc.stats.consecutive_failures = 0
            acc.stats.last_success_at = now
            if acc.status != "active" and acc.enabled:
                acc.status = "active"
                acc.cooldown_until = 0.0
            if acc.in_flight > 0:
                acc.in_flight -= 1
            self._write_unlocked()

    async def report_failure(self, account_id: str, error: str) -> None:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return
            now = time.time()
            acc.stats.failure_count += 1
            acc.stats.consecutive_failures += 1
            acc.stats.last_error = error
            acc.stats.last_error_at = now
            if acc.in_flight > 0:
                acc.in_flight -= 1

            err_lower = error.lower()
            is_auth_err = any(k in err_lower for k in (
                "401", "unauthorized", "forbidden", "403", "csrf", "invalid authentication",
            ))

            if is_auth_err and self.invalid_on_auth_error:
                acc.status = "invalid"
                acc.enabled = False
                logger.warning("Account %s marked INVALID (auth error): %s", acc.name, error)
            elif acc.stats.consecutive_failures >= self.max_consecutive_failures:
                acc.status = "cooldown"
                acc.cooldown_until = now + self.cooldown_seconds
                logger.warning("Account %s COOLDOWN after %d failures",
                               acc.name, acc.stats.consecutive_failures)
            self._write_unlocked()

    # ---------------- Pool-level config ----------------

    async def set_strategy(self, strategy: str) -> str:
        async with self._lock:
            if strategy in SCHEDULE_STRATEGIES:
                self.strategy = strategy
                self._write_unlocked()
            return self.strategy

    async def set_config(self, cooldown_seconds: Optional[int] = None,
                         max_consecutive_failures: Optional[int] = None,
                         invalid_on_auth_error: Optional[bool] = None) -> None:
        async with self._lock:
            if cooldown_seconds is not None:
                self.cooldown_seconds = cooldown_seconds
            if max_consecutive_failures is not None:
                self.max_consecutive_failures = max_consecutive_failures
            if invalid_on_auth_error is not None:
                self.invalid_on_auth_error = invalid_on_auth_error
            self._write_unlocked()

    async def get_config(self) -> Dict:
        async with self._lock:
            return {
                "strategy": self.strategy,
                "cooldown_seconds": self.cooldown_seconds,
                "max_consecutive_failures": self.max_consecutive_failures,
                "invalid_on_auth_error": self.invalid_on_auth_error,
                "total_accounts": len(self._accounts),
                "active_accounts": sum(
                    1 for a in self._accounts.values() if a.is_available(time.time())
                ),
            }

    async def get_summary(self) -> Dict:
        async with self._lock:
            now = time.time()
            total_req = sum(a.stats.total_requests for a in self._accounts.values())
            total_succ = sum(a.stats.success_count for a in self._accounts.values())
            total_fail = sum(a.stats.failure_count for a in self._accounts.values())
            by_status: Dict[str, int] = {}
            for a in self._accounts.values():
                st = a.status if a.is_available(now) and a.status == "active" else (
                    "disabled" if not a.enabled else
                    "cooldown" if a.status == "cooldown" and now < a.cooldown_until else
                    a.status
                )
                by_status[st] = by_status.get(st, 0) + 1
            return {
                "total_accounts": len(self._accounts),
                "by_status": by_status,
                "total_requests": total_req,
                "total_success": total_succ,
                "total_failure": total_fail,
                "in_flight": sum(a.in_flight for a in self._accounts.values()),
                "success_rate": round(total_succ / total_req * 100, 2) if total_req else 0.0,
            }
