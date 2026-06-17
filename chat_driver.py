#!/usr/bin/env python3
"""
GitLab Duo Proxy — Browser Chat Driver
=======================================

用 Playwright 驱动真实的 GitLab Duo Chat UI 发送消息，绕过"未知发送 mutation"问题。

工作原理:
  1. 用账号 Cookie 打开 Duo Chat 页面 (https://gitlab.com/-/duo_chat)
  2. 监听 /api/graphql 响应，拦截包含 workflow gid 的返回
  3. 在真实输入框里输入消息并点发送 (GitLab 前端自己处理发送协议)
  4. 拿到 workflow_id 后，用【已抓包验证可用】的 getWorkflowLatestCheckpoint 查询轮询
  5. 检测到新的 agent 消息 → 转成 OpenAI SSE chunk yield
  6. 工作流终态 → yield done

依赖: playwright (已在 browser_login 部署时装好)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import AsyncGenerator, Dict, List, Optional

logger = logging.getLogger("chat_driver")

WORKFLOW_GID_RE = re.compile(r"gid://gitlab/Ai::DuoWorkflows::Workflow/\d+")

DUO_CHAT_URL_PATH = "/-/duo_chat"
DASHBOARD_URL = "/dashboard/home"

# 已抓包验证可用的查询
QUERY_GET_WORKFLOW_CHECKPOINT = """
query getWorkflowLatestCheckpoint($workflowId: AiDuoWorkflowsWorkflowID!) {
  duoWorkflowWorkflows(workflowId: $workflowId) {
    nodes {
      id
      status
      workflowDefinition
      latestCheckpoint {
        workflowStatus
        errors
        duoMessages {
          content
          messageType
          messageId
          status
          timestamp
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}
"""


class BrowserChatDriver:
    """每次聊天创建一个临时 Playwright 页面，发完即销毁。"""

    def __init__(self, base_url: str = "https://gitlab.com", timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._pw = None
        self._browser = None

    async def _ensure_browser(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        return self._browser

    async def close(self):
        try:
            if self._browser: await self._browser.close()
        except Exception: pass
        try:
            if self._pw: await self._pw.stop()
        except Exception: pass
        self._browser = None
        self._pw = None

    def _cookies_from_str(self, cookie_str: str, domain: str) -> List[Dict]:
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name, value = name.strip(), value.strip()
            if not name: continue
            cookies.append({
                "name": name, "value": value,
                "domain": domain, "path": "/",
                "httpOnly": False, "secure": True, "sameSite": "Lax",
            })
        return cookies

    async def chat_stream(
        self,
        cookie_str: str,
        prompt: str,
        model_name: str = "claude-opus-4.8",
    ) -> AsyncGenerator[str, None]:
        """
        发送 prompt 并流式 yield OpenAI 兼容 SSE 字符串。
        """
        import httpx

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created_ts = int(time.time())

        def chunk(delta_content: str = "", finish_reason: Optional[str] = None, role: bool = False):
            d = {}
            if role: d["role"] = "assistant"
            if delta_content: d["content"] = delta_content
            return f"data: {json.dumps({'id':completion_id,'object':'chat.completion.chunk','created':created_ts,'model':model_name,'choices':[{'index':0,'delta':d,'finish_reason':finish_reason}]})}\n\n"

        yield chunk(role=True)

        # 解析域名
        from urllib.parse import urlparse
        host = urlparse(self.base_url).hostname or "gitlab.com"
        domain = "." + host.split(".", 1)[-1] if host.count(".") >= 1 else host

        page = None
        context = None
        captured_workflow_id = None
        captured_csrf = ""

        async def on_response(resp):
            nonlocal captured_workflow_id
            try:
                if "/api/graphql" not in resp.url: return
                if resp.request.method != "POST": return
                body = await resp.text()
                m = WORKFLOW_GID_RE.search(body)
                if m:
                    gid = m.group(0)
                    # 只取第一个（发送动作产生的 workflow）
                    if captured_workflow_id is None:
                        captured_workflow_id = gid
                        logger.info("[chat_driver] captured workflow_id=%s from graphql response", gid)
            except Exception:
                pass

        try:
            await self._ensure_browser()
            context = await self._browser.new_context(
                viewport={"width": 1100, "height": 760},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                locale="zh-CN", ignore_https_errors=True,
            )
            cookies = self._cookies_from_str(cookie_str, domain)
            if cookies: await context.add_cookies(cookies)
            page = await context.new_page()
            page.on("response", on_response)

            # 打开 Duo Chat 全屏页
            chat_url = self.base_url + DUO_CHAT_URL_PATH
            try:
                await page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                await page.goto(self.base_url + DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)

            # 抓 CSRF token
            try:
                captured_csrf = await page.evaluate(
                    "() => (document.querySelector('meta[name=csrf-token]')||{}).content || ''"
                )
            except Exception:
                captured_csrf = ""

            # 等待并定位输入框
            textarea = await self._find_input(page)
            if textarea is None:
                yield chunk("[Proxy Error] 找不到 Duo Chat 输入框，可能未登录或页面结构变化", finish_reason="error")
                yield "data: [DONE]\n\n"
                return

            # 输入并发送
            await textarea.fill(prompt)
            await asyncio.sleep(0.3)
            sent = await self._send(page, textarea)
            if not sent:
                yield chunk("[Proxy Error] 发送失败", finish_reason="error")
                yield "data: [DONE]\n\n"
                return

            # 等 workflow_id 被拦截（最多 15s）
            for _ in range(30):
                if captured_workflow_id: break
                await asyncio.sleep(0.5)

            if not captured_workflow_id:
                yield chunk("[Proxy Error] 未能捕获 workflow_id", finish_reason="error")
                yield "data: [DONE]\n\n"
                return

            # 用已知可用查询轮询回复
            async for evt in self._poll_response(captured_workflow_id, captured_csrf, cookie_str, host):
                if evt["type"] == "content":
                    yield chunk(evt["text"])
                elif evt["type"] == "done":
                    yield chunk(finish_reason="stop")
                    yield "data: [DONE]\n\n"
                    return
                elif evt["type"] == "error":
                    yield chunk("[Proxy Error] " + evt["text"], finish_reason="error")
                    yield "data: [DONE]\n\n"
                    return

            # 超时兜底
            yield chunk(finish_reason="stop")
            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("chat_driver error")
            yield chunk(f"[Proxy Error] {e}", finish_reason="error")
            yield "data: [DONE]\n\n"
        finally:
            try:
                if page: await page.close()
                if context: await context.close()
            except Exception:
                pass

    async def _find_input(self, page):
        """定位 Duo Chat 输入框，兼容多种选择器。"""
        selectors = [
            "[data-testid='duo-chat-question-input']",
            "textarea[placeholder*='ask']",
            "textarea[placeholder*='GitLab Duo']",
            "textarea[aria-label*='Duo']",
            "#duo-chat-question-input",
            "[contenteditable='true'][role='textbox']",
            "textarea",
        ]
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=4000)
                if el:
                    # 确认可见
                    box = await el.bounding_box()
                    if box: return el
            except Exception:
                continue
        return None

    async def _send(self, page, textarea) -> bool:
        """尝试多种发送方式。"""
        # 方式1: 点发送按钮
        for sel in ["[data-testid='duo-chat-send-button']", "button[type='submit']",
                    "button[aria-label*='Send']", "button[aria-label*='send']"]:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    return True
            except Exception:
                continue
        # 方式2: 在 textarea 上按 Enter
        try:
            await textarea.press("Enter")
            return True
        except Exception:
            return False

    async def _poll_response(
        self, workflow_id: str, csrf: str, cookie_str: str, host: str
    ) -> AsyncGenerator[Dict, None]:
        """用 getWorkflowLatestCheckpoint 轮询，yield 事件。"""
        import httpx

        url = self.base_url + "/api/graphql"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": self.base_url,
            "Referer": self.base_url + "/-/duo_chat",
            "X-Gitlab-Feature-Category": "duo_agent_platform",
            "Cookie": cookie_str,
        }
        if csrf:
            headers["X-Csrf-Token"] = csrf

        seen: set = set()
        deadline = time.time() + self.timeout
        payload = {
            "operationName": "getWorkflowLatestCheckpoint",
            "query": QUERY_GET_WORKFLOW_CHECKPOINT.strip(),
            "variables": {"workflowId": workflow_id},
        }

        rounds = 0
        while time.time() < deadline:
            rounds += 1
            await asyncio.sleep(1.0)
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    data = resp.json()
            except Exception as e:
                logger.debug("poll error r%d: %s", rounds, e)
                continue

            nodes = (data.get("data", {}) or {}).get("duoWorkflowWorkflows", {}).get("nodes", [])
            if not nodes:
                continue
            node = nodes[0]
            cp = node.get("latestCheckpoint") or {}
            messages = cp.get("duoMessages", []) or []
            status = cp.get("workflowStatus", "") or node.get("status", "")

            for msg in messages:
                mid = msg.get("messageId", "") or str(msg.get("timestamp", "")) + msg.get("messageType", "")
                if mid in seen:
                    continue
                seen.add(mid)
                mtype = msg.get("messageType", "")
                content = msg.get("content", "")
                if mtype == "agent" and content:
                    yield {"type": "content", "text": content}

            if status in ("COMPLETED", "FINISHED", "FAILED", "ERROR"):
                errs = cp.get("errors", [])
                if errs:
                    yield {"type": "error", "text": "; ".join(map(str, errs))}
                yield {"type": "done"}
                return

        yield {"type": "error", "text": "轮询超时"}


# 全局单例（复用 browser 进程）
_driver: Optional[BrowserChatDriver] = None
_driver_lock = asyncio.Lock()

async def get_driver(base_url: str = "https://gitlab.com") -> BrowserChatDriver:
    global _driver
    async with _driver_lock:
        if _driver is None:
            _driver = BrowserChatDriver(base_url=base_url, timeout=120)
        return _driver

async def close_driver():
    global _driver
    async with _driver_lock:
        if _driver:
            await _driver.close()
            _driver = None
