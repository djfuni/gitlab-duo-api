#!/usr/bin/env python3
"""
GitLab Duo Proxy — Browser Login Assistant
===========================================

在后端启动一个 Playwright headless Chromium（单例共享），通过 Context 隔离
多个登录/聊天会话。画面截图通过 WebSocket 推送到前端，前端把鼠标点击 /
键盘输入转发回来，在真实浏览器里重放。

依赖: playwright (需 `playwright install chromium` + `playwright install-deps`)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Awaitable, Callable, Dict, List, Optional, AsyncGenerator

logger = logging.getLogger("browser_login")

GITLAB_SIGN_IN_PATH = "/users/sign_in"
VIEWPORT_DEFAULT = (1024, 680)


class BrowserLoginSession:
    """单个浏览器登录/聊天 会话：一个 Context + Page + 截图循环 + 输入转发。

    不持有 Playwright 实例 / Browser — 由 BrowserLoginManager 统一管理，
    避免每次请求创建新的 chromium 进程泄漏。
    """

    def __init__(
        self,
        sid: str,
        base_url: str = "https://gitlab.com",
        viewport=VIEWPORT_DEFAULT,
        on_logged_in: Optional[Callable[[str], Awaitable[None]]] = None,
        skip_nav: bool = False,
    ):
        self.sid = sid
        self.base_url = base_url.rstrip("/")
        self.viewport = viewport
        self.on_logged_in = on_logged_in
        self.skip_nav = skip_nav  # 用于聊天临时会话：不加 cookie 前不导航
        self._context = None
        self.pinned_account_id: Optional[str] = None
        self.page = None
        self._latest_frame: bytes = b""
        self._frame_lock = asyncio.Lock()
        self._screenshot_task: Optional[asyncio.Task] = None
        self._closed = False
        self.current_url = ""
        self.title = ""
        self.logged_in = False
        self.status = "starting"
        self.error = ""
        self.created_at = time.time()

    async def start(self, browser) -> None:
        """在共享 browser 上创建 context + page。"""
        try:
            self._context = await browser.new_context(
                viewport={"width": self.viewport[0], "height": self.viewport[1]},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                ignore_https_errors=True,
            )
            self.page = await self._context.new_page()
            self.page.on("framenavigated", self._on_nav)
            if not self.skip_nav:
                await self.page.goto(self.base_url + GITLAB_SIGN_IN_PATH,
                                     wait_until="domcontentloaded", timeout=30000)
            self.current_url = self.page.url
            try:
                self.title = await self.page.title()
            except Exception:
                pass
            self.status = "ready"
            self._screenshot_task = asyncio.create_task(self._screenshot_loop())
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            logger.exception("BrowserLoginSession start failed")

    async def _on_nav(self, frame) -> None:
        try:
            if self.page and frame == self.page.main_frame:
                self.current_url = frame.url
                try:
                    self.title = await self.page.title()
                except Exception:
                    pass
                await self._check_login()
        except Exception:
            pass

    async def _check_login(self) -> None:
        if self.logged_in or self._closed or not self._context:
            return
        try:
            cookies = await self._context.cookies()
            has_session = any(
                c.get("name") == "_gitlab_session" and c.get("value") for c in cookies
            )
            on_login_page = GITLAB_SIGN_IN_PATH in self.current_url
            if has_session and not on_login_page:
                self.logged_in = True
                self.status = "logged_in"
                if self.on_logged_in:
                    try:
                        cookie_str = BrowserLoginManager._cookies_to_str(cookies)
                        await self.on_logged_in(cookie_str)
                    except Exception:
                        logger.exception("on_logged_in callback failed")
        except Exception:
            pass

    async def _screenshot_loop(self) -> None:
        while not self._closed and self.page:
            try:
                img = await self.page.screenshot(type="jpeg", quality=62)
                async with self._frame_lock:
                    self._latest_frame = img
            except Exception:
                pass
            await asyncio.sleep(0.32)

    async def stop_screenshot(self) -> None:
        """停止截图循环（pinned 会话不再需要实时截图）。"""
        if self._screenshot_task:
            self._screenshot_task.cancel()
            self._screenshot_task = None

    async def get_frame_b64(self) -> str:
        async with self._frame_lock:
            return base64.b64encode(self._latest_frame).decode("ascii") if self._latest_frame else ""

    async def click(self, x: int, y: int) -> None:
        if self.page and not self._closed:
            try:
                await self.page.mouse.click(x, y)
                await asyncio.sleep(0.05)
                await self._check_login()
            except Exception as e:
                logger.debug("click error: %s", e)

    async def type_text(self, text: str) -> None:
        if self.page and not self._closed:
            try:
                await self.page.keyboard.type(text, delay=25)
            except Exception as e:
                logger.debug("type error: %s", e)

    async def press_key(self, key: str) -> None:
        if self.page and not self._closed:
            try:
                await self.page.keyboard.press(key)
                await asyncio.sleep(0.05)
                await self._check_login()
            except Exception as e:
                logger.debug("key error: %s", e)

    async def scroll(self, dx: int, dy: int) -> None:
        if self.page and not self._closed:
            try:
                await self.page.mouse.wheel(dx, dy)
            except Exception:
                pass

    async def goto(self, url: str) -> None:
        if self.page and not self._closed:
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                logger.debug("goto error: %s", e)

    async def reload(self) -> None:
        if self.page and not self._closed:
            try:
                await self.page.reload(wait_until="domcontentloaded")
            except Exception:
                pass

    async def get_cookies_str(self) -> str:
        if not self._context:
            return ""
        cookies = await self._context.cookies()
        return BrowserLoginManager._cookies_to_str(cookies)

    async def get_cookie_names(self) -> List[str]:
        if not self._context:
            return []
        cookies = await self._context.cookies()
        return [c["name"] for c in cookies]

    # ---------------- 聊天驱动 ----------------
    async def chat_stream(
        self, prompt: str, model_name: str = "claude-opus-4.8", timeout: int = 120
    ) -> AsyncGenerator[str, None]:
        """驱动 GitLab Duo Chat UI 发送消息并流式返回 OpenAI SSE。"""
        import httpx
        import re as _re

        completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created_ts = int(time.time())

        def mk(delta: str = "", finish=None, role=False):
            import json as _j
            d: Dict = {}
            if role: d["role"] = "assistant"
            if delta: d["content"] = delta
            return "data: " + _j.dumps({"id": completion_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": model_name,
                "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}) + "\n\n"

        yield mk(role=True)

        if not self.page or self._closed:
            yield mk("[Proxy Error] 会话已关闭", finish="error")
            yield "data: [DONE]\n\n"; return

        gid_re = _re.compile(r"gid://gitlab/Ai::DuoWorkflows::Workflow/\d+")
        captured_wid: List[str] = []

        async def on_resp(resp):
            try:
                if "/api/graphql" not in resp.url or resp.request.method != "POST": return
                body = await resp.text()
                m = gid_re.search(body)
                if m and not captured_wid:
                    captured_wid.append(m.group(0))
                    logger.info("[chat] captured workflow_id=%s", m.group(0))
            except Exception: pass

        self.page.on("response", on_resp)
        try:
            is_home = "/dashboard/home" in (self.current_url or "") or "/dashboard/" in (self.current_url or "")
            if not is_home:
                try:
                    await self.page.goto(self.base_url + "/dashboard/home",
                                         wait_until="domcontentloaded", timeout=30000)
                except Exception: pass
                await asyncio.sleep(2)

            # 点开聊天面板
            try:
                toggle = await self.page.wait_for_selector('[data-testid="ai-chat-toggle"]', timeout=8000)
                await toggle.click(); await asyncio.sleep(2)
            except Exception:
                logger.debug("[chat] ai-chat-toggle not found")

            textarea = None
            for sel in ["[data-testid='chat-prompt-input']", "textarea[placeholder*='work' i]",
                        "textarea[aria-label*='Chat prompt' i]", "textarea"]:
                try:
                    el = await self.page.wait_for_selector(sel, timeout=5000)
                    b = await el.bounding_box() if el else None
                    if b and b.get("width", 0) > 60: textarea = el; break
                except Exception: continue
            if textarea is None:
                yield mk("[Proxy Error] 找不到 Duo Chat 输入框", finish="error")
                yield "data: [DONE]\n\n"; return

            await textarea.fill(prompt); await asyncio.sleep(0.3)
            sent = False
            for sel in ['[aria-label="Send chat message."]', "[data-testid='ai-send-button']",
                        'button[aria-label*="Send" i]']:
                try:
                    btn = await self.page.query_selector(sel)
                    if btn: await btn.click(); sent = True; break
                except Exception: continue
            if not sent:
                try: await textarea.press("Enter"); sent = True
                except Exception: pass
            if not sent:
                yield mk("[Proxy Error] 发送失败", finish="error")
                yield "data: [DONE]\n\n"; return

            for _ in range(30):
                if captured_wid: break
                await asyncio.sleep(0.5)
            if not captured_wid:
                yield mk("[Proxy Error] 未能拦截到 workflow_id", finish="error")
                yield "data: [DONE]\n\n"; return

            wid = captured_wid[0]
            cookie_str = await self.get_cookies_str()
            csrf = ""
            try:
                csrf = await self.page.evaluate(
                    "() => (document.querySelector('meta[name=csrf-token]')||{}).content || ''")
            except Exception: pass

            query = "query getWorkflowLatestCheckpoint($workflowId: AiDuoWorkflowsWorkflowID!) { duoWorkflowWorkflows(workflowId: $workflowId) { nodes { id status latestCheckpoint { workflowStatus errors duoMessages { content messageType messageId status timestamp __typename } __typename } __typename } __typename } }"
            headers = {"Content-Type": "application/json", "Accept": "application/json",
                       "Origin": self.base_url, "Referer": self.base_url + "/dashboard/home",
                       "X-Gitlab-Feature-Category": "duo_agent_platform", "Cookie": cookie_str}
            if csrf: headers["X-Csrf-Token"] = csrf
            seen: set = set()
            deadline = time.time() + timeout
            payload = {"operationName": "getWorkflowLatestCheckpoint", "query": query,
                       "variables": {"workflowId": wid}}
            while time.time() < deadline:
                await asyncio.sleep(1.0)
                try:
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False) as cl:
                        r = await cl.post(self.base_url + "/api/graphql", json=payload, headers=headers)
                        data = r.json()
                except Exception: continue
                nodes = (data.get("data", {}) or {}).get("duoWorkflowWorkflows", {}).get("nodes", [])
                if not nodes: continue
                node = nodes[0]; cp = node.get("latestCheckpoint") or {}
                msgs = cp.get("duoMessages", []) or []
                status = cp.get("workflowStatus", "") or node.get("status", "")
                for m in msgs:
                    mid = m.get("messageId", "") or str(m.get("timestamp", "")) + m.get("messageType", "")
                    if mid in seen: continue
                    seen.add(mid)
                    if m.get("messageType") == "agent" and m.get("content"):
                        yield mk(m["content"])
                if status in ("COMPLETED", "FINISHED", "FAILED", "ERROR"):
                    errs = cp.get("errors", []) or []
                    if errs:
                        yield mk("[Proxy Error] " + "; ".join(map(str, errs)), finish="error")
                    else: yield mk(finish="stop")
                    yield "data: [DONE]\n\n"; return
            yield mk("[Proxy Error] 轮询超时", finish="error")
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.exception("chat_stream error")
            yield mk(f"[Proxy Error] {e}", finish="error")
            yield "data: [DONE]\n\n"
        finally:
            try: self.page.remove_listener("response", on_resp)
            except Exception: pass

    async def close(self) -> None:
        """仅关闭 context，不关 browser。"""
        if self._closed: return
        self._closed = True
        self.status = "closed"
        if self._screenshot_task:
            self._screenshot_task.cancel()
        try:
            if self._context: await self._context.close()
        except Exception: pass
        self._context = None
        self.page = None


class BrowserLoginManager:
    """浏览器会话管理器 — 全局共享一个 Playwright 实例和 Browser。

    每个 BrowserLoginSession 只创建 Context（隔离 cookies/存储），
    避免每次请求创建新的 chromium 进程。
    """

    def __init__(self, max_sessions: int = 5, session_ttl: int = 600):
        self._sessions: Dict[str, BrowserLoginSession] = {}
        self._pinned: Dict[str, BrowserLoginSession] = {}
        self._lock = asyncio.Lock()
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl
        self._pw = None
        self._browser = None
        self._browser_lock = asyncio.Lock()

    async def _ensure_browser(self):
        async with self._browser_lock:
            if self._browser is None or not self._browser.is_connected():
                # 清理旧连接
                if self._browser:
                    try: await self._browser.close()
                    except Exception: pass
                if self._pw:
                    try: await self._pw.stop()
                    except Exception: pass
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox",
                          "--disable-dev-shm-usage", "--disable-gpu",
                          "--font-render-hinting=none"],
                )
                logger.info("[mgr] shared browser started")

    async def create(
        self, sid: str, base_url: str = "https://gitlab.com",
        on_logged_in: Optional[Callable[[str], Awaitable[None]]] = None,
        skip_nav: bool = False,
    ) -> BrowserLoginSession:
        await self._ensure_browser()
        async with self._lock:
            await self._gc_unlocked()
            old = self._sessions.pop(sid, None)
            if old: await old.close()
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("too many concurrent login sessions")
            sess = BrowserLoginSession(sid, base_url=base_url,
                                       on_logged_in=on_logged_in,
                                       skip_nav=skip_nav)
            self._sessions[sid] = sess
        await sess.start(self._browser)
        return sess

    def get(self, sid: str) -> Optional[BrowserLoginSession]:
        return self._sessions.get(sid)

    async def close(self, sid: str) -> None:
        async with self._lock:
            sess = self._sessions.pop(sid, None)
            if sess and sess.pinned_account_id:
                self._sessions[sid] = sess; return
        if sess: await sess.close()

    def pin_for_account(self, account_id: str, session: BrowserLoginSession) -> None:
        session.pinned_account_id = account_id
        self._pinned[account_id] = session
        # pinned 会话不需要截图了
        asyncio.create_task(session.stop_screenshot())

    def get_pinned(self, account_id: str) -> Optional[BrowserLoginSession]:
        s = self._pinned.get(account_id)
        if s and not s._closed: return s
        return None

    async def close_pinned(self, account_id: str) -> None:
        async with self._lock:
            sess = self._pinned.pop(account_id, None)
        if sess: await sess.close()

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values()) + list(self._pinned.values())
            self._sessions.clear(); self._pinned.clear()
        for s in sessions: await s.close()
        async with self._browser_lock:
            if self._browser:
                try: await self._browser.close()
                except Exception: pass
            if self._pw:
                try: await self._pw.stop()
                except Exception: pass
            self._browser = None; self._pw = None

    async def _gc_unlocked(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if (now - s.created_at > self.session_ttl or s._closed) and not s.pinned_account_id
        ]
        for sid in expired:
            s = self._sessions.pop(sid, None)
            if s:
                asyncio.create_task(s.close())

    @staticmethod
    def _cookies_to_str(cookies: List[Dict]) -> str:
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
