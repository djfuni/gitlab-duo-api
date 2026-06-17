# GitLab Duo Chat → OpenAI 兼容 API 代理

## 🎯 项目概述

将 **GitLab Duo Chat** 的在线对话接口逆向并转换为 **OpenAI 格式的 API 接口**，完全兼容 OpenAI SDK 和所有支持 OpenAI API 格式的工具（如 LlamaIndex、LangChain、Cursor、Continue 等）。

### 核心能力

| 功能 | 状态 | 说明 |
|------|------|------|
| `/v1/chat/completions` | ✅ | 完全兼容 OpenAI Chat Completions API |
| SSE 流式响应 | ✅ | 支持 `stream: true` |
| 多模型切换 | ✅ | Claude Opus/Sonnet/Haiku, GitLab Duo 等 |
| Cookie/Token 认证 | ✅ | 支持运行时切换账号 |
| 对话历史管理 | ✅ | 通过 conversation_id 续接对话 |
| 错误处理 & 重试 | ✅ | 完整的错误传递机制 |

---

## 🏗️ 协议架构 (逆向分析结果)

### 发现日期: 2026-06-17
### 分析方法: 浏览器网络拦截器注入 + 实时抓包

```
┌─────────────┐     GraphQL POST      ┌──────────────────┐
│             │ ──────────────────▶   │                  │
│  本地代理    │    /api/graphql       │  GitLab 后端      │
│  (server.py)│                       │  (gitlab.com)    │
│             │ ◀──────────────────   │                  │
│             │   Workflow Checkpoint │  Duo Workflow     │
│             │   (轮询消息列表)       │  System           │
└─────────────┘                       └──────────────────┘
      │
      ▼
┌─────────────┐
│ OpenAI SDK  │  ← 客户端看到的是标准 OpenAI API
│ / curl      │
└─────────────┘
```

### 关键协议细节

#### 1. 端点与认证
```
POST https://gitlab.com/api/graphql
Content-Type: application/json

Headers:
  X-Csrf-Token: <从页面meta标签获取>
  X-Gitlab-Feature-Category: duo_agent_platform
  X-Gitlab-Version: 19.1.0-pre
  Cookie: _gitlab_session=<session_value>  (或其他认证方式)
```

#### 2. 工作流系统 (核心发现!)
GitLab Duo Chat 不是简单的 request-response，而是基于 **Duo Workflow 系统**：

```graphql
# 轮询查询 — 获取工作流中的消息列表
query getWorkflowLatestCheckpoint($workflowId: AiDuoWorkflowsWorkflowID!) {
  duoWorkflowWorkflows(workflowId: $workflowId) {
    nodes {
      id                    # 工作流 ID: gid://gitlab/Ai::DuoWorkflows::Workflow/{数字}
      status                # INPUT_REQUIRED | PROCESSING | COMPLETED | FAILED
      workflowDefinition    # "chat" 表示聊天模式
      latestCheckpoint {
        workflowGoal        # 用户的问题/prompt
        workflowStatus      # 当前状态
        errors              # 错误信息(如有)
        duoMessages [        # 消息列表!
          content            # 消息内容
          messageType        # "user" | "agent"
          messageId          # 唯一消息ID
          timestamp          # ISO时间戳
          additionalContext  # 附加上下文(当前页面URL等)
        ]
      }
    }
  }
}
```

#### 3. 消息结构
```json
{
  "content": "What is 2+2?",
  "messageType": "user",
  "messageId": "user-74f89723-a698-4bbe-8fba-873b40bb542c",
  "timestamp": "2026-06-17T05:00:17.799436+00:00",
  "status": "success",
  "additionalContext": [{
    "category": "REPOSITORY",
    "id": "page-context",
    "content": "<current_gitlab_page_url>https://gitlab.com/dashboard/home</current_gitlab_page_url>"
  }]
}
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
cd gitlab-duo-api
pip install -r requirements.txt
```

### 2. 配置认证

编辑 `config.yaml`:

```yaml
gitlab:
  auth_type: "cookie"        # cookie | token | session | oauth
  auth_value: "_gitlab_session=你的session值; 其他cookie..."
```

**获取 Cookie 方法：**
1. 打开 https://gitlab.com 并登录
2. 按 F12 打开开发者工具
3. 切换到 Network 标签
4. 刷新页面或发送任意请求
5. 点击任意一个请求 → Headers → 找到 `Cookie:` 行
6. 复制完整的 Cookie 值

或者通过环境变量（优先级更高）：

```bash
export GITLAB_AUTH_TYPE="cookie"
export GITLAB_AUTH_VALUE="_gitlab_session=xxx; ..."
export GITLAB_BASE_URL="https://gitlab.com"
```

### 3. 启动服务

```bash
python server.py
# 服务将在 http://localhost:8080 启动
# Swagger 文档: http://localhost:8080/docs
```

### 4. 调用测试

```bash
# 方式 A: OpenAI SDK (推荐)
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="any-value-or-your-gitlab-cookie"
)
response = client.chat.completions.create(
    model="claude-opus-4.8",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)
for chunk in response:
    print(chunk.choices[0].delta.content, end="")

# 方式 B: curl
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4.8","messages":[{"role":"user","content":"Hi"}],"stream":true}'

# 方式 C: 运行测试脚本
python test_client.py
```

---

## 📡 API 参考

### `POST /v1/chat/completions` — 聊天补全

**Request Body (OpenAI 标准):**
```json
{
  "model": "claude-opus-4.8",           // 模型名 (必填或使用默认值)
  "messages": [                          // 消息列表 (必填)
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "stream": false,                       // 是否流式 (默认 false)
  "temperature": 0.7,                    // 温度参数 (可选)
  "max_tokens": 4096,                    // 最大token数 (可选)
  "conversation_id": null,               // GitLab 工作流ID，用于续接对话 (可选)
  "resource": null                       // GitLab 资源上下文 (可选)
}
```

**Response (非流式):**
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1718580000,
  "model": "claude-opus-4.8",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "回复内容..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 20,
    "total_tokens": 30
  }
}
```

**Response (流式 SSE):**
```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

### `GET /v1/models` — 列出模型

### `POST /v1/accounts/switch` — 运行时切换账号

```bash
curl -X POST http://localhost:8080/v1/accounts/switch \
  -H "Content-Type: application/json" \
  -d '{"auth_type": "cookie", "auth_value": "new_cookie_string"}'
```

### `GET /v1/accounts/info` — 查看当前账号状态

---

## 🔧 支持的模型

| 模型名称 | GitLab 内部 ID | 提供商 |
|---------|---------------|--------|
| `claude-opus-4.8` | anthropic/claude-opus-4.8 | Anthropic |
| `claude-sonnet-4` | anthropic/claude-sonnet-4 | Anthropic |
| `claude-haiku-3.5` | anthropic/claude-haiku-3.5 | Anthropic |
| `gitlab-duo` | gitlab_duo | GitLab |
| `duo-chat` | duo_chat | GitLab |

可在 `config.yaml` 中添加更多模型映射。

---

## 🔐 认证方式说明

### 方式 1: Cookie (推荐)
```yaml
auth_type: "cookie"
auth_value: "_gitlab_session=abc123; _gitlab_session_random=xyz789; ..."
```
完整复制浏览器 Cookie 字符串即可。

### 方式 2: Personal Access Token
```yaml
auth_type: "token"
auth_value: "glpat-xxxxxxxxxxxxxxxxxxxx"
```
在 GitLab → Settings → Access Tokens 创建。

### 方式 3: Session Token
```yaml
auth_type: "session"
auth_value: "仅 _gitlab_session 的值"
```

### 方式 4: OAuth Bearer Token
```yaml
auth_type: "oauth"
auth_value: "oauth2 access_token"
```

### Per-Request Auth Override
也可以在每个请求的 `Authorization` header 中传入不同的认证信息：
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer 另一个账号的cookie或token" \
  -d '{"model":"claude-opus-4.8","messages":[{"role":"user","content":"Hi"}]}'
```

---

## 🛠️ 高级用法

### 在 Cursor / Continue IDE 中使用

Settings → OpenAI API Key → 填入任意值  
Settings → OpenAI Base URL → `http://localhost:8080/v1`

### 在 LangChain 中使用

```python
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(
    base_url="http://localhost:8080/v1",
    model="claude-opus-4.8",
    api_key="proxy-key"
)
response = llm.invoke("Hello!")
```

### 在 LlamaIndex 中使用

```python
from llama_index.llms.openai import OpenAI
llm = OpenAI(
    base_url="http://localhost:8080/v1",
    model="claude-opus-4.8",
    api_key="proxy-key"
)
```

### Docker 部署

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["python", "server.py"]
```

```bash
docker build -t gitlab-duo-proxy .
docker run -p 8080:8080 \
  -e GITLAB_AUTH_TYPE=cookie \
  -e GITLAB_AUTH_VALUE="_gitlab_session=xxx" \
  gitlab-duo-proxy
```

---

## ⚠️ 注意事项

1. **CSRF Token**: 服务启动时会自动尝试获取 CSRF Token。如果失败，需手动在 config.yaml 中设置。
2. **速率限制**: GitLab 有 API 速率限制，频繁调用可能触发限制。
3. **会话过期**: Cookie/Session 可能会过期，需要定期更新。
4. **隐私**: 此工具仅在本地运行，不会将任何数据发送到第三方服务器。
5. **合规性**: 请遵守 GitLab 的服务条款，不要滥用 API。

---

## 📁 文件结构

```
gitlab-duo-api/
├── server.py              # 主服务 (FastAPI 应用)
├── config.yaml            # 配置文件模板
├── requirements.txt       # Python 依赖
├── test_client.py         # 测试客户端
├── capture_network.js     # 网络请求拦截器 (浏览器注入脚本)
├── capture_api.html       # 拦截器使用说明 HTML
├── QUICKSTART.md          # 本文件 (快速开始指南)
└── README.md              # 项目简介
```

---

## 📄 License

MIT License - 仅供学习和研究使用
