#!/usr/bin/env python3
"""
GitLab Duo Chat → OpenAI Compatible API Proxy (v2)
====================================================

基于真实逆向分析的 GitLab Duo Chat 协议构建。

协议发现 (2026-06-17 通过浏览器网络拦截验证):
==================================================
1. GitLab Duo Chat 使用 GraphQL 端点: POST /api/graphql
2. 聊天基于 Duo Workflow 系统 (Ai::DuoWorkflows::Workflow)
3. 消息通过 GraphQL mutation 发送到工作流
4. 响应通过 getWorkflowLatestCheckpoint 查询轮询
5. 工作流状态: INPUT_REQUIRED → processing → complete
6. 消息类型: user(用户) / agent(AI助手)
7. 认证: Cookie (_gitlab_session) + CSRF Token

功能：
- /v1/chat/completions — 完全兼容 OpenAI SDK
- 支持流式响应 (SSE)
- 多模型切换
- Cookie/Token 切换账号
- 对话历史管理 (conversation_id = workflow_id)

使用方式：
    pip install -r requirements.txt
    python server.py
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid
import logging
import secrets
from dataclasses import dataclass, field, asdict
from typing import Optional, AsyncGenerator, Dict, List, Any, Callable, Awaitable
from pathlib import Path

try:
    from fastapi import FastAPI, Request, HTTPException, Header, Body, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, FileResponse, RedirectResponse
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
    import httpx
    import yaml
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

# Account pool + browser login (local modules)
sys.path.insert(0, str(Path(__file__).parent))
from account_pool import AccountPool, Account, SCHEDULE_STRATEGIES  # noqa: E402
from browser_login import BrowserLoginManager, BrowserLoginSession  # noqa: E402
from chat_driver import get_driver, close_driver  # noqa: E402
from api_keys import ApiKeyManager  # noqa: E402


# ============================================================
# Configuration
# ============================================================

CONFIG_PATH = Path(__file__).parent / "config.yaml"

DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "debug": False,
    },
    "gitlab": {
        "base_url": "https://gitlab.com",
        "auth_type": "cookie",       # cookie | token | session | oauth
        "auth_value": "",
        "graphql_endpoint": "/api/graphql",
        "timeout": 120,
        "default_model": "claude-opus-4.8",
        # CSRF token (auto-fetched or manually set)
        "csrf_token": "",
        # Polling interval for workflow checkpoint (seconds)
        "poll_interval": 1.0,
        # Max polling rounds before timeout
        "max_poll_rounds": 180,
    },
    "models": {
        "claude-opus-4.8": {"id": "anthropic/claude-opus-4.8", "provider": "anthropic"},
        "claude-sonnet-4": {"id": "anthropic/claude-sonnet-4", "provider": "anthropic"},
        "claude-haiku-3.5": {"id": "anthropic/claude-haiku-3.5", "provider": "anthropic"},
        "gpt-5.5": {"id": "openai/gpt-5.5", "provider": "openai"},
        "gitlab-duo": {"id": "gitlab_duo", "provider": "gitlab"},
        "duo-chat": {"id": "duo_chat", "provider": "gitlab"},
    },
}


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False
    gitlab_base_url: str = "https://gitlab.com"
    auth_type: str = "cookie"
    auth_value: str = ""
    graphql_endpoint: str = "/api/graphql"
    timeout: int = 120
    default_model: str = "claude-opus-4.8"
    csrf_token: str = ""
    poll_interval: float = 1.0
    max_poll_rounds: int = 180
    models: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Account pool
    pool_enabled: bool = True
    pool_strategy: str = "round_robin"
    pool_cooldown_seconds: int = 60
    pool_max_failures: int = 3
    pool_retry_count: int = 3
    pool_invalid_on_auth_error: bool = True
    # WebUI access token (auto-generated if empty; protects management UI)
    webui_token: str = ""
    # Allow requests without auth header to use pool (vs require Authorization)
    allow_anonymous_pool: bool = True


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    cfg_dict = DEFAULT_CONFIG.copy()
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            user_cfg = yaml.safe_load(f) or {}
        for section, values in user_cfg.items():
            if section in cfg_dict and isinstance(cfg_dict[section], dict):
                cfg_dict[section].update(values)
            else:
                cfg_dict[section] = values

    env_map = {
        ("server", "host"): "GITLAB_PROXY_HOST",
        ("server", "port"): "GITLAB_PROXY_PORT",
        ("gitlab", "auth_type"): "GITLAB_AUTH_TYPE",
        ("gitlab", "auth_value"): "GITLAB_AUTH_VALUE",
        ("gitlab", "base_url"): "GITLAB_BASE_URL",
        ("gitlab", "default_model"): "GITLAB_DEFAULT_MODEL",
        ("gitlab", "csrf_token"): "GITLAB_CSRF_TOKEN",
    }
    for (section, key), env_var in env_map.items():
        val = os.environ.get(env_var)
        if val is not None:
            if key == "port":
                val = int(val)
            cfg_dict[section][key] = val

    sc = cfg_dict["server"]
    gc = cfg_dict["gitlab"]
    pool = cfg_dict.get("pool", {})
    return AppConfig(
        host=sc.get("host", "0.0.0.0"),
        port=sc.get("port", 8080),
        debug=sc.get("debug", False),
        gitlab_base_url=gc.get("base_url", "https://gitlab.com"),
        auth_type=gc.get("auth_type", "cookie"),
        auth_value=gc.get("auth_value", ""),
        graphql_endpoint=gc.get("graphql_endpoint", "/api/graphql"),
        timeout=gc.get("timeout", 120),
        default_model=gc.get("default_model", "claude-opus-4.8"),
        csrf_token=gc.get("csrf_token", ""),
        poll_interval=gc.get("poll_interval", 1.0),
        max_poll_rounds=gc.get("max_poll_rounds", 180),
        models=cfg_dict.get("models", DEFAULT_CONFIG["models"]),
        pool_enabled=pool.get("enabled", True),
        pool_strategy=pool.get("strategy", "round_robin"),
        pool_cooldown_seconds=pool.get("cooldown_seconds", 60),
        pool_max_failures=pool.get("max_failures", 3),
        pool_retry_count=pool.get("retry_count", 3),
        pool_invalid_on_auth_error=pool.get("invalid_on_auth_error", True),
        webui_token=pool.get("webui_token", "") or cfg_dict.get("webui_token", ""),
        allow_anonymous_pool=pool.get("allow_anonymous", True),
    )


# ============================================================
# Pydantic Models (OpenAI-compatible)
# ============================================================

class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = ""
    messages: List[ChatMessage] = Field(..., description="Chat messages")
    stream: bool = False
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    stop: Optional[List[str]] = None
    presence_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    user: Optional[str] = None
    conversation_id: Optional[str] = Field(
        default=None,
        description="Existing GitLab Workflow ID (gid://gitlab/Ai::DuoWorkflows::Workflow/xxx) to continue conversation"
    )
    resource: Optional[str] = Field(default=None, description="GitLab resource context")


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: Optional[ChoiceMessage] = None
    delta: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str = ""
    object: str = "chat.completion"
    created: int = 0
    model: str = ""
    choices: List[ChatCompletionChoice] = []
    usage: Optional[UsageInfo] = None


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "gitlab-duo"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo] = []


# ============================================================
# GitLab Duo Chat Protocol Client (v2 - Workflow Based)
# ============================================================

class GitLabDuoClientV2:
    """
    GitLab Duo Chat API 客户端 v2
    
    基于真实逆向分析的协议：
    
    === 协议流程 ===
    1. 发送消息: GraphQL mutation → 创建/更新 Duo Workflow
    2. 轮询响应: getWorkflowLatestCheckpoint 查询 → 获取消息列表
    3. 流式输出: 将轮询到的增量内容转换为 SSE 格式
    
    === 关键发现 (2026-06-17) ===
    - 端点: POST /api/graphql
    - 系统: Ai::DuoWorkflows::Workflow
    - 查询: getWorkflowLatestCheckpoint($workflowId)
    - 消息结构: latestCheckpoint.duoMessages[]
    - 请求头: x-csrf-token, x-gitlab-feature-category=duo_agent_platform
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.base_url = config.gitlab_base_url.rstrip("/")
        self.graphql_url = f"{self.base_url}{config.graphql_endpoint}"

    def _build_headers(self, override_auth: Optional[str] = None) -> Dict[str, str]:
        """构建请求头（包含认证和GitLab特定头）"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "GitLab-Duo-Proxy/2.0",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/dashboard/home",
            "X-Gitlab-Feature-Category": "duo_agent_platform",
            "X-Gitlab-Version": "19.1.0-pre",
        }

        # CSRF token
        if self.config.csrf_token:
            headers["X-Csrf-Token"] = self.config.csrf_token

        # Auth
        auth_value = override_auth or self.config.auth_value
        if self.config.auth_type == "cookie":
            headers["Cookie"] = auth_value
        elif self.config.auth_type == "token":
            headers["PRIVATE-TOKEN"] = auth_value
            headers["Authorization"] = f"Bearer {auth_value}"
        elif self.config.auth_type == "session":
            headers["Cookie"] = f"_gitlab_session={auth_value}"
        elif self.config.auth_type == "oauth":
            headers["Authorization"] = f"Bearer {auth_value}"

        return headers

    async def _fetch_csrf_token(self) -> str:
        """从 GitLab 页面获取 CSRF token"""
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(f"{self.base_url}/dashboard/home")
            match = re.search(r'name="csrf-token" content="([^"]+)"', resp.text)
            if match:
                return match.group(1)
            # Try meta tag pattern
            match = re.search(r'csrf-token.*?content="([^"]+)"', resp.text)
            if match:
                return match.group(1)
        return ""

    async def _graphql_request(
        self,
        operation_name: str,
        query: str,
        variables: Dict[str, Any],
        override_auth: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行 GraphQL 请求"""
        payload = {
            "operationName": operation_name,
            "query": query.strip(),
            "variables": variables,
        }

        headers = self._build_headers(override_auth)

        async with httpx.AsyncClient(timeout=self.config.timeout, follow_redirects=True) as client:
            resp = await client.post(self.graphql_url, json=payload, headers=headers)
            result = resp.json()

            if "errors" in result:
                errors = [e.get("message", "Unknown") for e in result["errors"]]
                raise Exception(f"GraphQL Error ({resp.status_code}): {'; '.join(errors)}")

            return result

    # ---- GraphQL Operations (based on reverse-engineered schema) ----

    QUERY_GET_WORKFLOW_CHECKPOINT = """
    query getWorkflowLatestCheckpoint($workflowId: AiDuoWorkflowsWorkflowID!) {
      duoWorkflowWorkflows(workflowId: $workflowId) {
        nodes {
          id
          status
          aiCatalogItemVersionId
          workflowDefinition
          archived
          stalled
          latestCheckpoint {
            workflowGoal
            workflowStatus
            errors
            duoMessages {
              content
              messageType
              messageSubType
              status
              toolInfo
              timestamp
              correlationId
              messageId
              role
              additionalContext {
                category
                id
                content
                metadata
                __typename
              }
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

    MUTATION_SEND_CHAT_MESSAGE = """
    mutation sendChatMessage($input: AiDuoWorkflowsSendMessageInput!) {
      sendDuoChatMessage(input: $input) {
        errors
        workflow {
          id
          status
          latestCheckpoint {
            workflowStatus
            duoMessages {
              messageId
              content
              messageType
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

    MUTATION_CREATE_WORKFLOW = """
    mutation createDuoWorkflow($input: CreateDuoWorkflowInput!) {
      createDuoWorkflow(input: $input) {
        errors
        workflow {
          id
          status
          __typename
        }
        __typename
      }
    }
    """

    # Fallback: direct aiAction mutation (older/simpler API path)
    MUTATION_AI_ACTION = """
    mutation aiAction($question: String!, $modelId: ModelID!, $conversationId: ConversationID, $resource: AiAgentResourceInput) {
      aiAction(input: { question: $question, modelId: $modelId, conversationId: $conversationId, resource: $resource }) {
        errors
        messageId
        requestId
        chatId
      }
    }
    """

    SUBSCRIPTION_AI_RESPONSE = """
    subscription aiMessageResponse($chatId: ID!, $requestId: String!) {
      aiMessageResponse(chatId: $chatId, requestId: $requestId) {
        ... on AiMessageType {
          id
          role
          content
          timestamp
          chunkId
        }
        ... on AiErrorMessage {
          message
          errorCode
        }
        ... on AiCompleteMessage {
          completionReason
        }
      }
    }
    """

    async def send_message_to_workflow(
        self,
        prompt: str,
        model_id: str,
        conversation_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        发送聊天消息到 GitLab Duo Workflow
        
        尝试多种 mutation 方式以兼容不同版本的 GitLab
        """
        
        # Strategy 1: Try sendDuoChatMessage mutation (preferred for newer GitLab)
        try:
            result = await self._graphql_request(
                operation_name="sendChatMessage",
                query=self.MUTATION_SEND_CHAT_MESSAGE,
                variables={
                    "input": {
                        "prompt": prompt,
                        "modelId": model_id,
                        "conversationId": conversation_id,
                    }
                },
            )
            data = result.get("data", {}).get("sendDuoChatMessage", {})
            if data.get("workflow"):
                wf = data["workflow"]
                return {
                    "workflow_id": wf["id"],
                    "status": wf["status"],
                    "method": "sendDuoChatMessage",
                }
        except Exception as e:
            logging.debug(f"sendDuoChatMessage failed: {e}")

        # Strategy 2: Try aiAction mutation (fallback)
        try:
            result = await self._graphql_request(
                operation_name="aiAction",
                query=self.MUTATION_AI_ACTION,
                variables={
                    "question": prompt,
                    "modelId": model_id,
                    "conversationId": conversation_id,
                },
            )
            data = result.get("data", {}).get("aiAction", {})
            if data.get("requestId"):
                return {
                    "request_id": data["requestId"],
                    "chat_id": data.get("chatId") or conversation_id,
                    "message_id": data.get("messageId"),
                    "method": "aiAction",
                }
        except Exception as e:
            logging.debug(f"aiAction failed: {e}")

        # Strategy 3: Create new workflow then poll
        try:
            result = await self._graphql_request(
                operation_name="createDuoWorkflow",
                query=self.MUTATION_CREATE_WORKFLOW,
                variables={
                    "input": {
                        "goal": prompt,
                        "definition": "chat",
                        "modelId": model_id,
                    }
                },
            )
            data = result.get("data", {}).get("createDuoWorkflow", {})
            if data.get("workflow"):
                wf = data["workflow"]
                return {
                    "workflow_id": wf["id"],
                    "status": wf["status"],
                    "method": "createDuoWorkflow",
                }
        except Exception as e:
            logging.debug(f"createDuoWorkflow failed: {e}")

        raise Exception("All message sending strategies failed")

    async def poll_workflow_response(
        self,
        workflow_id: str,
        last_message_count: int = 0,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        轮询工作流检查点，yield 新增的消息
        
        基于 getWorkflowLatestCheckpoint 查询
        """
        round_num = 0
        seen_message_ids = set()

        while round_num < self.config.max_poll_rounds:
            round_num += 1
            await asyncio.sleep(self.config.poll_interval)

            try:
                result = await self._graphql_request(
                    operation_name="getWorkflowLatestCheckpoint",
                    query=self.QUERY_GET_WORKFLOW_CHECKPOINT,
                    variables={"workflowId": workflow_id},
                )
                
                nodes = (
                    result
                    .get("data", {})
                    .get("duoWorkflowWorkflows", {})
                    .get("nodes", [])
                )

                if not nodes:
                    continue

                node = nodes[0]
                checkpoint = node.get("latestCheckpoint")
                if not checkpoint:
                    continue

                messages = checkpoint.get("duoMessages", [])
                status = checkpoint.get("workflowStatus", "")
                errors = checkpoint.get("errors", [])

                # Yield new messages
                new_messages = []
                for msg in messages:
                    msg_id = msg.get("messageId", "")
                    if msg_id and msg_id not in seen_message_ids:
                        seen_message_ids.add(msg_id)
                        new_messages.append(msg)

                for msg in new_messages:
                    yield {
                        "type": "message",
                        "message": msg,
                        "workflow_status": status,
                    }

                # Check terminal states
                if status in ("COMPLETED", "FINISHED", "FAILED", "ERROR"):
                    if errors:
                        yield {
                            "type": "error",
                            "errors": errors,
                            "workflow_status": status,
                        }
                    yield {
                        "type": "done",
                        "workflow_status": status,
                        "total_messages": len(messages),
                    }
                    return

                # Also check node-level status
                node_status = node.get("status", "")
                if node_status in ("COMPLETED", "FINISHED", "FAILED"):
                    yield {
                        "type": "done",
                        "workflow_status": node_status,
                        "total_messages": len(messages),
                    }
                    return

            except Exception as e:
                yield {
                    "type": "poll_error",
                    "error": str(e),
                    "round": round_num,
                }

        # Timeout
        yield {
            "type": "timeout",
            "rounds": round_num,
        }

    async def stream_chat(
        self,
        messages: List[ChatMessage],
        model_name: str,
        conversation_id: Optional[str] = None,
        override_auth: Optional[str] = None,
        on_started: Optional[Callable[[str], Awaitable[None]]] = None,
        on_send_error: Optional[Callable[[str], Awaitable[None]]] = None,
        raise_on_send_error: bool = False,
        emit_initial_role: bool = True,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """
        完整的流式聊天流程:
        1. 构建 prompt
        2. 发送消息到工作流
        3. 轮询响应并转换为 OpenAI SSE 格式

        钩子 (供账号池使用):
        - on_started(workflow_id): 发送成功、开始轮询前调用
        - on_send_error(error_msg): 发送阶段失败时调用
        - raise_on_send_error=True 时, 发送失败直接抛出异常 (不 yield error chunk),
          便于外层捕获后切换账号重试。此时 emit_initial_role 自动置为 False。
        """
        if raise_on_send_error:
            emit_initial_role = False

        model_info = self._resolve_model(model_name)
        model_id = model_info["id"]
        prompt = self._build_prompt(messages)

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created_ts = int(time.time())

        # Initial role chunk (deferred until after send succeeds when raise_on_send_error)
        initial_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": model_name,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }],
        }
        if emit_initial_role:
            yield f"data: {json.dumps(initial_chunk)}\n\n"

        _send_succeeded = False
        try:
            # Step 1: Send message
            send_result = await self.send_message_to_workflow(
                prompt=prompt,
                model_id=model_id,
                conversation_id=conversation_id,
                **kwargs,
            )

            workflow_id = send_result.get("workflow_id") or send_result.get("chat_id")

            # If we don't have a workflow_id, we can't poll
            if not workflow_id:
                # For aiAction method, try subscription-style response
                request_id = send_result.get("request_id")
                if request_id and send_result.get("chat_id"):
                    if on_started:
                        await on_started(send_result["chat_id"])
                    _send_succeeded = True
                    if not emit_initial_role:
                        yield f"data: {json.dumps(initial_chunk)}\n\n"
                    async for chunk in self._stream_ai_action_response(
                        request_id, send_result["chat_id"], completion_id, created_ts, model_name
                    ):
                        yield chunk
                    return

                raise Exception(f"No workflow/chat ID in response: {send_result}")

            # Send succeeded
            _send_succeeded = True
            if on_started:
                await on_started(workflow_id)
            if not emit_initial_role:
                yield f"data: {json.dumps(initial_chunk)}\n\n"

            # Step 2: Poll for response
            full_content_parts = []
            async for event in self.poll_workflow_response(workflow_id):
                etype = event.get("type")

                if etype == "message":
                    msg = event["message"]
                    content = msg.get("content", "")
                    mtype = msg.get("messageType", "")

                    # Only forward agent/assistant messages as content chunks
                    if mtype == "agent" and content:
                        full_content_parts.append(content)
                        
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model_name,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": content},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                elif etype == "error":
                    err_content = "\n\n[Error] " + "; ".join(event.get("errors", []))
                    err_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model_name,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": err_content},
                            "finish_reason": "error",
                        }],
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"

                elif etype == "done":
                    # Final done chunk
                    done_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model_name,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }],
                    }
                    yield f"data: {json.dumps(done_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                elif etype == "timeout":
                    timeout_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model_name,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": "\n\n[Timeout waiting for response]"},
                            "finish_reason": "length",
                        }],
                    }
                    yield f"data: {json.dumps(timeout_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

        except Exception as e:
            # Send-phase failure: notify pool and optionally re-raise for retry
            if not _send_succeeded:
                if on_send_error:
                    try:
                        await on_send_error(str(e))
                    except Exception:
                        pass
                if raise_on_send_error:
                    raise
            error_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model_name,
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"\n\n[Proxy Error] {str(e)}"},
                    "finish_reason": "error",
                }],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"

    async def _stream_ai_action_response(
        self, request_id: str, chat_id: str, 
        completion_id: str, created_ts: int, model_name: str,
    ) -> AsyncGenerator[str, None]:
        """Fallback: use subscription-style polling for aiAction responses"""
        # Poll using the subscription query as a regular query
        for i in range(self.config.max_poll_rounds):
            await asyncio.sleep(self.config.poll_interval)
            try:
                result = await self._graphql_request(
                    operation_name="aiMessageResponse",
                    query=self.SUBSCRIPTION_AI_RESPONSE,
                    variables={"chatId": chat_id, "requestId": request_id},
                )
                # Subscription via regular POST won't work well, but let's try
                data = result.get("data", {}).get("aiMessageResponse")
                if data:
                    content = data.get("content", "")
                    if content:
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    
                    reason = data.get("completionReason") or data.get("errorCode")
                    if reason:
                        done_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }
                        yield f"data: {json.dumps(done_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
            except Exception:
                pass
        
        yield "data: [DONE]\n\n"

    def _resolve_model(self, model_name: str) -> Dict[str, str]:
        if not model_name:
            model_name = self.config.default_model
        if model_name in self.config.models:
            return self.config.models[model_name]
        lower = model_name.lower()
        for k, v in self.config.models.items():
            if lower == k.lower():
                return v
        return {"id": model_name, "provider": "unknown"}

    def _build_prompt(self, messages: List[ChatMessage]) -> str:
        parts = []
        for msg in messages:
            role = msg.role.upper()
            content = msg.content or ""
            if role == "SYSTEM":
                parts.append(f"[System Instructions]\n{content}")
            elif role == "USER":
                parts.append(content)
            elif role == "ASSISTANT":
                parts.append(f"[Previous Assistant Response]\n{content}")
            elif role == "TOOL":
                parts.append(f"[Tool Result]\n{content}")
        return "\n\n".join(parts)


# ============================================================
# FastAPI Application
# ============================================================

app = FastAPI(
    title="GitLab Duo Chat → OpenAI API Proxy v2",
    version="2.0.0",
    description="Convert GitLab Duo Chat (Duo Workflow) to OpenAI-compatible API. "
                "Based on real protocol reverse-engineering of gitlab.com.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config: Optional[AppConfig] = None
client: Optional[GitLabDuoClientV2] = None
pool: Optional[AccountPool] = None
login_mgr: Optional[BrowserLoginManager] = None
api_key_mgr: Optional[ApiKeyManager] = None

# Storage for the account pool
POOL_STORAGE_PATH = Path(__file__).parent / "accounts.json"
API_KEYS_STORAGE_PATH = Path(__file__).parent / "api_keys.json"
WEB_DIR = Path(__file__).parent / "web"


@app.on_event("startup")
async def startup():
    global config, client, pool, login_mgr, api_key_mgr
    config = load_config()

    # Auto-fetch CSRF token if not provided
    if not config.csrf_token and config.auth_type in ("cookie", "session"):
        logging.info("Auto-fetching CSRF token...")
        temp_client = GitLabDuoClientV2(config)
        config.csrf_token = await temp_client._fetch_csrf_token()
        if config.csrf_token:
            logging.info(f"CSRF token fetched: {config.csrf_token[:16]}...")
        else:
            logging.warning("Could not auto-fetch CSRF token. Set it manually in config.yaml.")

    client = GitLabDuoClientV2(config)

    # Initialize account pool
    pool = AccountPool(
        storage_path=POOL_STORAGE_PATH,
        strategy=config.pool_strategy,
        cooldown_seconds=config.pool_cooldown_seconds,
        max_consecutive_failures=config.pool_max_failures,
        invalid_on_auth_error=config.pool_invalid_on_auth_error,
    )
    await pool.load()

    # Initialize browser login manager
    login_mgr = BrowserLoginManager(max_sessions=5, session_ttl=600)

    # Initialize API key manager
    api_key_mgr = ApiKeyManager(API_KEYS_STORAGE_PATH)
    await api_key_mgr.load()

    # Auto-generate a WebUI access token if none set
    if not config.webui_token:
        config.webui_token = secrets.token_urlsafe(16)
        logging.info(f"[WebUI] Auto-generated access token: {config.webui_token}")

    logging.basicConfig(level=logging.DEBUG if config.debug else logging.INFO)
    logging.info("=" * 60)
    logging.info("  GitLab Duo Proxy v2 (Workflow-Based) + Account Pool")
    logging.info(f"  Listening: http://{config.host}:{config.port}")
    logging.info(f"  WebUI:     http://{config.host}:{config.port}/web")
    logging.info(f"  WebUI Token: {config.webui_token}")
    logging.info(f"  Auth Type: {config.auth_type}")
    logging.info(f"  Base URL: {config.gitlab_base_url}")
    logging.info(f"  Models:    {', '.join(config.models.keys())}")
    pool_cfg = await pool.get_config()
    logging.info(f"  Pool: enabled={config.pool_enabled} strategy={pool_cfg['strategy']} "
                 f"accounts={pool_cfg['total_accounts']} active={pool_cfg['active_accounts']}")
    logging.info("=" * 60)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gitlab-duo-proxy-v2", "version": "2.0.0"}


@app.get("/v1/models", response_model=ModelListResponse)
async def list_models():
    models = [
        ModelInfo(id=k, owned_by=v.get("provider", "gitlab"))
        for k, v in config.models.items()
    ]
    return ModelListResponse(data=models)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, authorization: Optional[str] = Header(None)):
    if not config or not client:
        raise HTTPException(status_code=503, detail="Service not initialized")

    req_auth = None
    is_api_key = False
    api_key_raw = None
    if authorization:
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else authorization
        if token.startswith("sk-"):
            is_api_key = True
            api_key_raw = token
        else:
            req_auth = token

    # API key auth: verify key, then use pool (key tracks usage)
    if is_api_key and api_key_mgr:
        key_obj = await api_key_mgr.verify(api_key_raw)
        if not key_obj or not key_obj.enabled:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        await api_key_mgr.report_usage(api_key_raw)
        # Fall through to pool — API key just authenticates, pool handles actual sending

    model = req.model or config.default_model
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_ts = int(time.time())

    # Decide auth source:
    #   1. Per-request Authorization (non-API-key) → use it directly (backward compatible)
    #   2. API key or anonymous → pool scheduling with retry
    #   3. Fallback → config.auth_value (original behavior)
    use_pool = (
        config.pool_enabled
        and pool is not None
        and not req_auth
        and (config.allow_anonymous_pool or is_api_key)
    )

    if use_pool:
        pool_summary = await pool.get_summary()
        active_in_pool = (pool_summary.get("by_status", {}) or {}).get("active", 0)
        if active_in_pool == 0:
            # No active account in pool → fall back to config auth if available
            if config.auth_value and config.auth_value.strip() not in ("", "_gitlab_session=YOUR_SESSION_HERE; _gitlab_session_random=..."):
                use_pool = False
            else:
                raise HTTPException(
                    status_code=503,
                    detail="账号池中没有可用账号。请先在 WebUI 通过「浏览器登录」或手动添加一个 GitLab 账号。",
                )

    if use_pool:
        async def pool_stream():
            tried: List[str] = []
            last_error = "No available account"
            # 把多轮 messages 合并成单个 prompt（Duo Chat UI 单消息发送）
            prompt_parts = []
            for m in req.messages:
                role = m.role.upper()
                c = m.content or ""
                if role == "SYSTEM":
                    prompt_parts.append(f"[System]\n{c}")
                elif role == "USER":
                    prompt_parts.append(c)
                elif role == "ASSISTANT":
                    prompt_parts.append(f"[Assistant]\n{c}")
            prompt = "\n\n".join(prompt_parts)

            for attempt in range(max(1, config.pool_retry_count)):
                account = await pool.acquire(exclude=tried)
                if account is None:
                    break
                tried.append(account.id)

                # 优先复用 pinned 会话，否则用 cookie 创建临时浏览器会话
                sess = login_mgr.get_pinned(account.id) if login_mgr else None
                is_tmp = False
                if sess is None:
                    # 创建临时会话：跳过初始登录页导航，直接设 cookie 后跳 dashboard
                    tmp_sid = uuid.uuid4().hex[:10]
                    try:
                        sess = await login_mgr.create(tmp_sid, base_url=config.gitlab_base_url, skip_nav=True)
                        is_tmp = True
                    except Exception as e:
                        last_error = f"创建浏览器会话失败: {e}"
                        logging.warning("[Pool] temp session create failed: %s", e)
                        await pool.report_failure(account.id, last_error)
                        continue
                    # 用账号 cookie 覆盖
                    from urllib.parse import urlparse as _up
                    host = _up(config.gitlab_base_url).hostname
                    domain = "." + ".".join(host.split(".")[-2:])
                    cookies_to_set = []
                    for pair in account.auth_value.split(";"):
                        pair = pair.strip()
                        if "=" not in pair: continue
                        n, _, v = pair.partition("=")
                        cookies_to_set.append({"name": n.strip(), "value": v.strip(),
                            "domain": domain, "path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax"})
                    try:
                        await sess._context.add_cookies(cookies_to_set)
                        await sess.page.goto(config.gitlab_base_url + "/dashboard/home",
                                             wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(2)
                    except Exception as e:
                        logging.warning("[Pool] cookie set/nav failed: %s", e)

                streamed_any = False
                try:
                    async for chunk in sess.chat_stream(
                        prompt=prompt, model_name=model,
                    ):
                        streamed_any = True
                        yield chunk
                    await pool.report_success(account.id)
                    return
                except Exception as e:
                    last_error = str(e)
                    logging.warning(
                        "[Pool] account '%s' chat failed (attempt %d/%d): %s",
                        account.name, attempt + 1, config.pool_retry_count, e,
                    )
                    await pool.report_failure(account.id, last_error)
                    if streamed_any:
                        yield "data: [DONE]\n\n"
                        return
                finally:
                    if is_tmp:
                        try: await sess.close()
                        except Exception: pass
                continue

            # All retries exhausted
            err_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"\n\n[Proxy Error] All pool accounts failed. Last error: {last_error}"},
                    "finish_reason": "error",
                }],
            }
            yield f"data: {json.dumps(err_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        if req.stream:
            return StreamingResponse(
                pool_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            full_content = []
            async for chunk_str in pool_stream():
                if chunk_str.startswith("data: ") and "[DONE]" not in chunk_str:
                    try:
                        data = json.loads(chunk_str[6:])
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        c = delta.get("content", "")
                        if c:
                            full_content.append(c)
                    except (json.JSONDecodeError, IndexError):
                        pass
            content = "".join(full_content)
            response = ChatCompletionResponse(
                id=completion_id, object="chat.completion", created=created_ts, model=model,
                choices=[ChatCompletionChoice(index=0,
                    message=ChoiceMessage(role="assistant", content=content),
                    finish_reason="stop" if content else "error")],
                usage=UsageInfo(
                    prompt_tokens=sum(len(m.content or "") // 4 for m in req.messages),
                    completion_tokens=len(content) // 4,
                    total_tokens=sum(len(m.content or "") // 4 for m in req.messages) + len(content) // 4,
                ),
            )
            return JSONResponse(content=json.loads(response.model_dump_json()))

    # ---- Non-pool path (per-request auth or config fallback) ----
    active_client = client
    if req_auth:
        active_client = GitLabDuoClientV2(AppConfig(**asdict(config), auth_value=req_auth))

    if req.stream:
        async def generate():
            async for chunk in active_client.stream_chat(
                messages=req.messages,
                model_name=model,
                conversation_id=req.conversation_id,
                override_auth=req_auth,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                yield chunk

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # Non-streaming: collect all chunks
        full_content = []
        async for chunk_str in active_client.stream_chat(
            messages=req.messages,
            model_name=model,
            conversation_id=req.conversation_id,
            override_auth=req_auth,
        ):
            if chunk_str.startswith("data: ") and "[DONE]" not in chunk_str:
                try:
                    data = json.loads(chunk_str[6:])
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    c = delta.get("content", "")
                    if c:
                        full_content.append(c)
                except (json.JSONDecodeError, IndexError):
                    pass

        content = "".join(full_content)
        response = ChatCompletionResponse(
            id=completion_id,
            object="chat.completion",
            created=created_ts,
            model=model,
            choices=[ChatCompletionChoice(
                index=0,
                message=ChoiceMessage(role="assistant", content=content),
                finish_reason="stop",
            )],
            usage=UsageInfo(
                prompt_tokens=sum(len(m.content or "") // 4 for m in req.messages),
                completion_tokens=len(content) // 4,
                total_tokens=sum(len(m.content or "") // 4 for m in req.messages) + len(content) // 4,
            ),
        )
        return JSONResponse(content=json.loads(response.model_dump_json()))


@app.post("/v1/accounts/switch")
async def switch_account(request: Request):
    global config, client
    body = await request.json()
    auth_type = body.get("auth_type", "cookie")
    auth_value = body.get("auth_value", "")
    if not auth_value:
        raise HTTPException(status_code=400, detail="auth_value required")
    
    config.auth_type = auth_type
    config.auth_value = auth_value
    client = GitLabDuoClientV2(config)
    
    return {
        "status": "ok",
        "message": f"Account switched (auth_type={auth_type})",
        "auth_preview": auth_value[:20] + "..." if len(auth_value) > 20 else auth_value,
    }


@app.get("/v1/accounts/info")
async def account_info():
    if not config:
        raise HTTPException(status_code=503, detail="Service not initialized")
    val = config.auth_value
    preview = val[:8] + "..." + val[-4:] if len(val) > 12 else "***"
    return {
        "auth_type": config.auth_type,
        "auth_value_preview": preview,
        "base_url": config.gitlab_base_url,
        "default_model": config.default_model,
        "csrf_token_set": bool(config.csrf_token),
        "available_models": list(config.models.keys()),
        "protocol_version": "v2-workflow",
    }


# ============================================================
# Account Pool Management API
# ============================================================

def _check_webui_token(request: Request) -> bool:
    """Verify WebUI management token from header or query."""
    if not config:
        return False
    token = (
        request.headers.get("x-webui-token")
        or request.query_params.get("token")
        or ""
    )
    return secrets.compare_digest(token, config.webui_token)


async def _require_webui(request: Request):
    if not _check_webui_token(request):
        raise HTTPException(status_code=401, detail="Invalid or missing WebUI token")
    return True


@app.get("/v1/accounts/pool")
async def pool_list(request: Request):
    """List all accounts in the pool."""
    await _require_webui(request)
    return {"accounts": await pool.list_all(mask=True)}


@app.get("/v1/accounts/pool/summary")
async def pool_summary(request: Request):
    """Pool-level statistics."""
    await _require_webui(request)
    return await pool.get_summary()


@app.get("/v1/accounts/pool/config")
async def pool_get_config(request: Request):
    await _require_webui(request)
    return await pool.get_config()


@app.put("/v1/accounts/pool/config")
async def pool_put_config(request: Request):
    await _require_webui(request)
    body = await request.json()
    await pool.set_config(
        cooldown_seconds=body.get("cooldown_seconds"),
        max_consecutive_failures=body.get("max_consecutive_failures"),
        invalid_on_auth_error=body.get("invalid_on_auth_error"),
    )
    if body.get("strategy"):
        await pool.set_strategy(body["strategy"])
    return {"status": "ok", "config": await pool.get_config()}


@app.post("/v1/accounts/pool")
async def pool_add(request: Request):
    """Add a new account to the pool."""
    await _require_webui(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    auth_type = (body.get("auth_type") or "cookie").strip()
    auth_value = (body.get("auth_value") or "").strip()
    if not name or not auth_value:
        raise HTTPException(status_code=400, detail="name and auth_value are required")
    if auth_type not in ("cookie", "token", "session", "oauth"):
        raise HTTPException(status_code=400, detail="invalid auth_type")
    acc = await pool.add(
        name=name, auth_type=auth_type, auth_value=auth_value,
        note=body.get("note", ""), enabled=body.get("enabled", True),
    )
    return {"status": "ok", "account": acc.to_dict(mask=True)}


@app.get("/v1/accounts/pool/{account_id}")
async def pool_get(account_id: str, request: Request):
    await _require_webui(request)
    acc = await pool.get(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    return {"account": acc.to_dict(mask=True)}


@app.put("/v1/accounts/pool/{account_id}")
async def pool_update(account_id: str, request: Request):
    await _require_webui(request)
    body = await request.json()
    acc = await pool.update(account_id, **body)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": acc.to_dict(mask=True)}


@app.delete("/v1/accounts/pool/{account_id}")
async def pool_delete(account_id: str, request: Request):
    await _require_webui(request)
    ok = await pool.delete(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "deleted": account_id}


@app.post("/v1/accounts/pool/{account_id}/reset")
async def pool_reset(account_id: str, request: Request):
    """Reset an account to active status (clear cooldown/invalid)."""
    await _require_webui(request)
    acc = await pool.reset_status(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": acc.to_dict(mask=True)}


@app.post("/v1/accounts/pool/{account_id}/test")
async def pool_test(account_id: str, request: Request):
    """
    Test an account by calling GitLab /api/v4/user.
    Returns 200 with user info on success, marks account invalid on auth failure.
    """
    await _require_webui(request)
    acc = await pool.get(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")

    base = config.gitlab_base_url.rstrip("/")
    headers = {"User-Agent": "GitLab-Duo-Proxy/2.0"}
    if acc.auth_type == "cookie":
        headers["Cookie"] = acc.auth_value
    elif acc.auth_type == "token":
        headers["PRIVATE-TOKEN"] = acc.auth_value
    elif acc.auth_type == "session":
        headers["Cookie"] = f"_gitlab_session={acc.auth_value}"
    elif acc.auth_type == "oauth":
        headers["Authorization"] = f"Bearer {acc.auth_value}"

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            resp = await http.get(f"{base}/api/v4/user", headers=headers)
        if resp.status_code == 200:
            user = resp.json()
            await pool.report_success(acc.id)
            return {
                "status": "ok",
                "account_id": acc.id,
                "user": {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "name": user.get("name"),
                    "email": user.get("email"),
                    "state": user.get("state"),
                },
            }
        else:
            err = f"HTTP {resp.status_code}"
            await pool.report_failure(acc.id, err)
            return JSONResponse(
                status_code=200,
                content={"status": "fail", "account_id": acc.id,
                         "http_code": resp.status_code,
                         "detail": resp.text[:300]},
            )
    except Exception as e:
        await pool.report_failure(acc.id, str(e))
        return JSONResponse(
            status_code=200,
            content={"status": "error", "account_id": acc.id, "detail": str(e)},
        )


@app.get("/v1/accounts/pool/token")
async def pool_get_token(request: Request):
    """Return the current WebUI token (used by the UI to bootstrap)."""
    await _require_webui(request)
    return {"token": config.webui_token, "pool_enabled": config.pool_enabled}


# ============================================================
# Browser Login Assistant (Playwright + WebSocket 串流)
# ============================================================

@app.post("/v1/accounts/pool/assist/create")
async def assist_create(request: Request):
    """启动一个新的浏览器登录会话，返回 sid。"""
    await _require_webui(request)
    if login_mgr is None:
        raise HTTPException(status_code=503, detail="login manager not initialized")
    sid = uuid.uuid4().hex[:12]
    try:
        # 会话在 WebSocket 连接时真正启动；这里仅预注册 sid 并检查 playwright 可用性
        import playwright  # noqa: F401
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="playwright not installed on server. Run: pip install playwright && playwright install chromium",
        )
    return {"sid": sid, "base_url": config.gitlab_base_url}


@app.websocket("/v1/accounts/pool/assist/ws")
async def assist_ws(ws: WebSocket, token: str = ""):
    """浏览器登录串流 WebSocket。

    前端消息:
      {type: "start"}                启动浏览器
      {type: "click", x, y}          点击
      {type: "type", text}           输入文本
      {type: "key", key}             按键 (Enter/Tab/Backspace/...)
      {type: "scroll", dx, dy}       滚动
      {type: "goto", url}            导航
      {type: "reload"}               刷新
      {type: "close"}                关闭

    后端消息:
      {type: "ready", sid, viewport}
      {type: "frame", data, url, title, logged_in, status}
      {type: "logged_in", cookie_preview}
      {type: "error", message}
    """
    # 鉴权 (query param)
    if not config or not secrets.compare_digest(token, config.webui_token):
        await ws.close(code=4401)
        return
    await ws.accept()

    sid = uuid.uuid4().hex[:12]
    sess: Optional[BrowserLoginSession] = None
    push_task: Optional[asyncio.Task] = None
    logged_in_notified = False

    async def on_logged_in(cookie_str: str) -> None:
        nonlocal logged_in_notified
        if not logged_in_notified:
            logged_in_notified = True
            preview = cookie_str[:32] + "..." if len(cookie_str) > 32 else cookie_str
            try:
                await ws.send_json({"type": "logged_in", "cookie_preview": preview})
            except Exception:
                pass

    try:
        # 等待前端 start 指令
        first = await ws.receive_json()
        if first.get("type") != "start":
            await ws.send_json({"type": "error", "message": "expected start first"})
            await ws.close()
            return

        sess = await login_mgr.create(sid, base_url=config.gitlab_base_url, on_logged_in=on_logged_in)
        await ws.send_json({
            "type": "ready",
            "sid": sid,
            "viewport": list(sess.viewport),
            "base_url": config.gitlab_base_url,
            "status": sess.status,
            "error": sess.error,
        })

        async def push_frames():
            while not sess._closed:
                frame = await sess.get_frame_b64()
                if frame:
                    await ws.send_json({
                        "type": "frame",
                        "data": frame,
                        "url": sess.current_url,
                        "title": sess.title,
                        "logged_in": sess.logged_in,
                        "status": sess.status,
                    })
                await asyncio.sleep(0.3)

        push_task = asyncio.create_task(push_frames())

        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if sess._closed:
                break
            if mtype == "click":
                await sess.click(int(msg.get("x", 0)), int(msg.get("y", 0)))
            elif mtype == "type":
                await sess.type_text(msg.get("text", ""))
            elif mtype == "key":
                await sess.press_key(msg.get("key", ""))
            elif mtype == "scroll":
                await sess.scroll(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
            elif mtype == "goto":
                await sess.goto(msg.get("url", ""))
            elif mtype == "reload":
                await sess.reload()
            elif mtype == "login":
                # 用 httpx 直接 POST 登录（绕过 CF），成功后 cookie 注入浏览器
                result = await sess.login_via_httpx(
                    username=msg.get("username", ""),
                    password=msg.get("password", ""),
                )
                await ws.send_json({"type": "login_result", **result})
            elif mtype == "close":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.exception("assist ws error")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if push_task:
            push_task.cancel()
        if sess and not sess.pinned_account_id:
            await login_mgr.close(sid)
        try:
            await ws.close()
        except Exception:
            pass


@app.post("/v1/accounts/pool/assist/{sid}/save")
async def assist_save(sid: str, request: Request):
    """从指定登录会话抓取 Cookie 并保存为新账号。"""
    await _require_webui(request)
    sess = login_mgr.get(sid) if login_mgr else None
    if not sess:
        raise HTTPException(status_code=404, detail="session not found or expired")
    if not sess.logged_in:
        # 兜底：再检查一次
        await sess._check_login()
        if not sess.logged_in:
            raise HTTPException(status_code=400, detail="not logged in yet")
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    cookie_str = await sess.get_cookies_str()
    if not cookie_str or "_gitlab_session" not in cookie_str:
        raise HTTPException(status_code=400, detail="no valid gitlab session cookie found")
    note = body.get("note", "").strip() or f"browser login · {sess.current_url}"
    acc = await pool.add(
        name=name, auth_type="cookie", auth_value=cookie_str, note=note,
    )
    # 把已登录会话钉住给该账号聊天用（Cloudflare 已过，复用同一浏览器上下文）
    login_mgr.pin_for_account(acc.id, sess)
    logging.info("[assist] session %s pinned for account %s (%s)", sid, acc.id, name)
    return {"status": "ok", "account": acc.to_dict(mask=True), "pinned": True}


@app.on_event("shutdown")
async def shutdown():
    if login_mgr:
        await login_mgr.close_all()
    await close_driver()


# ============================================================
# API Key Management
# ============================================================

@app.get("/v1/api-keys")
async def api_keys_list(request: Request):
    """列出所有 API 密钥。"""
    await _require_webui(request)
    return {"keys": await api_key_mgr.list_all_full()}


@app.post("/v1/api-keys")
async def api_keys_create(request: Request):
    """生成新的 API 密钥。返回原始密钥（仅此一次可见）。"""
    await _require_webui(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    raw_key = await api_key_mgr.create(name=name, note=body.get("note", ""))
    return {"status": "ok", "key": raw_key, "message": "请立即复制保存，关闭后无法再次查看完整密钥"}


@app.delete("/v1/api-keys/{key_id}")
async def api_keys_revoke(key_id: str, request: Request):
    """吊销（禁用）API 密钥。"""
    await _require_webui(request)
    ok = await api_key_mgr.revoke(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found")
    return {"status": "ok", "revoked": key_id}


@app.put("/v1/api-keys/{key_id}")
async def api_keys_rename(key_id: str, request: Request):
    await _require_webui(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    ok = await api_key_mgr.rename(key_id, name)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found")
    return {"status": "ok", "renamed": key_id}


# ============================================================


# ============================================================
# 真实浏览器登录代理 (反向代理 gitlab.com 捕获 Cookie)
# ============================================================

# 待捕获的 session (sid -> captured cookies)
_auth_sessions: Dict[str, Dict] = {}
_auth_lock = asyncio.Lock()

AUTH_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>GitLab 登录授权</title>
<base href="__PROXY_BASE__/auth/proxy/">
<style>
  body{margin:0;font-family:-apple-system,sans-serif;}
  .bar{display:flex;align-items:center;justify-content:space-between;padding:10px 18px;background:#1f1e1d;color:#f5f4ed;font-size:13px;}
  .bar .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;}
  .bar .dot.off{background:#6b6a65;} .bar .dot.on{background:#5a8f5a;}
  .bar button{background:#d97757;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;}
  iframe{width:100%;height:calc(100vh - 44px);border:none;}
  .done{display:none;text-align:center;padding:60px;}
  .done h2{color:#5a8f5a;}
</style></head><body>
<div class="bar">
  <span><span id="dot" class="dot off"></span><span id="hint">正在连接 GitLab，请登录…</span></span>
  <button id="saveBtn" style="display:none" onclick="doSave()">保存此账号</button>
</div>
<iframe id="frm" src="__PROXY_BASE__/auth/proxy/users/sign_in"></iframe>
<section class="done" id="doneWrap"><h2>登录成功</h2><p>输入账号名称保存到账号池：</p>
  <input id="accName" style="padding:8px;width:240px;margin:10px;" placeholder="账号名称"><br>
  <button onclick="doSave()" style="background:#d97757;color:#fff;border:none;padding:10px 24px;border-radius:6px;cursor:pointer;">保存</button>
</section>
<script>
var sid="__SID__";
var token="__TOKEN__";
var api="__PROXY_BASE__";
var checked=0;
setInterval(function(){
  checked++;
  fetch(api+'/auth/check?sid='+sid+'&token='+token).then(r=>r.json()).then(d=>{
    if(d.logged_in){
      document.getElementById('dot').className='dot on';
      document.getElementById('hint').textContent='已登录 GitLab - 可以保存了';
      document.getElementById('saveBtn').style.display='inline-block';
    } else if(checked>2){
      document.getElementById('hint').textContent='请在上方窗口中登录 GitLab';
    }
  });
},5000);
function doSave(){
  var name=prompt('账号名称：');
  if(!name)return;
  fetch(api+'/auth/save?sid='+sid+'&token='+token,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})})
    .then(r=>r.json()).then(d=>{
      if(d.status==='ok'){document.getElementById('doneWrap').style.display='block';
        document.querySelector('.bar').innerHTML='<span style="color:#5a8f5a;">已保存到账号池</span>';
        setTimeout(function(){window.close();},3000);
      }else{alert('保存失败: '+(d.error||d.detail));}
    });
}
</script></body></html>"""


async def _proxy_fetch(url: str, method: str, headers: Dict, body: bytes = b"") -> httpx.Response:
    proxy_headers = {k: v for k, v in headers.items()
                     if k.lower() not in ("host", "accept-encoding")}
    proxy_headers["accept-encoding"] = "identity"
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, verify=False) as cl:
        return await cl.request(method, url, headers=proxy_headers, content=body or None)


@app.api_route("/auth/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def auth_proxy(request: Request, path: str):
    """反向代理 gitlab.com。用 cookie 里的 auth_sid 鉴权。"""
    sid = request.cookies.get("auth_sid", "")
    if not sid:
        raise HTTPException(status_code=401)
    async with _auth_lock:
        if sid not in _auth_sessions:
            raise HTTPException(status_code=401, detail="session not found")
    qs = f"?{request.url.query}" if request.url.query else ""
    target = f"{config.gitlab_base_url.rstrip('/')}/{path}{qs}"
    body = await request.body()
    resp = await _proxy_fetch(target, request.method, dict(request.headers), body)
    set_cookies = resp.headers.get_list("set-cookie")
    if set_cookies:
        async with _auth_lock:
            session = _auth_sessions.setdefault(sid, {"cookies": {}, "logged": False, "url": ""})
            for sc in set_cookies:
                for part in sc.split(","):
                    part = part.strip()
                    if "=" in part:
                        n, _, v = part.partition("=")
                        n = n.strip(); v = v.split(";")[0].strip()
                        session["cookies"][n] = v
            if resp.status_code in (302, 303) and "location" in resp.headers:
                loc = resp.headers["location"]
                if "/dashboard" in loc and "/sign_in" not in loc:
                    session["logged"] = True; session["url"] = loc
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type and resp.status_code == 200:
        html = resp.text
        proxy_base = f"{request.url.scheme}://{request.url.netloc}"
        base_tag = f'<base href="{proxy_base}/auth/proxy/">'
        if "<head>" in html:
            html = html.replace("<head>", f"<head>\n{base_tag}", 1)
        elif "<html" in html:
            html = html.replace("<html", f'<html>\n<head>{base_tag}</head>', 1)
        return HTMLResponse(html, status_code=resp.status_code, headers=dict(resp.headers))
    return HTMLResponse(resp.content, status_code=resp.status_code, headers=dict(resp.headers))


@app.get("/auth/start")
async def auth_start(token: str = "", request: Request = None):
    if not secrets.compare_digest(token, config.webui_token):
        raise HTTPException(status_code=401)
    sid = uuid.uuid4().hex[:12]
    # 创建空 session 并设 cookie 方便后续代理请求鉴权
    async with _auth_lock:
        _auth_sessions[sid] = {"cookies": {}, "logged": False, "url": ""}
    # 使用请求里的实际 host（而非 config 里的 0.0.0.0）
    proxy_base = f"{request.url.scheme}://{request.url.netloc}" if request else f"http://{config.host}:{config.port}"
    html = AUTH_HTML.replace("__SID__", sid).replace("__TOKEN__", token).replace("__PROXY_BASE__", proxy_base)
    resp = HTMLResponse(html)
    resp.set_cookie("auth_sid", sid, path="/", samesite="lax")
    return resp


@app.get("/auth/check")
async def auth_check(sid: str, token: str = ""):
    if not secrets.compare_digest(token, config.webui_token):
        return {"logged_in": False}
    async with _auth_lock:
        s = _auth_sessions.get(sid, {})
    return {"logged_in": s.get("logged", False)}


@app.post("/auth/save")
async def auth_save(sid: str, token: str = "", request: Request = None):
    if not secrets.compare_digest(token, config.webui_token):
        raise HTTPException(401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, detail="name required")
    async with _auth_lock:
        s = _auth_sessions.pop(sid, {})
    cookies = s.get("cookies", {})
    cookie_str = "; ".join(f"{n}={v}" for n, v in cookies.items())
    if "_gitlab_session" not in cookie_str:
        return {"status": "error", "error": "未检测到 _gitlab_session cookie"}
    acc = await pool.add(name=name, auth_type="cookie", auth_value=cookie_str,
                         note=f"proxy auth - {s.get('url','')}")
    return {"status": "ok", "account": acc.to_dict(mask=True)}


# WebUI (Claude-style)
# ============================================================

@app.get("/web", response_class=HTMLResponse)
@app.get("/web/", response_class=HTMLResponse)
async def webui_index():
    index = WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>WebUI not built</h1><p>web/index.html missing</p>", status_code=404)
    return HTMLResponse(index.read_text(encoding="utf-8"))


if WEB_DIR.exists():
    app.mount("/web/static", StaticFiles(directory=str(WEB_DIR)), name="web-static")


if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    uvicorn.run(app, host=cfg.host, port=cfg.port)
