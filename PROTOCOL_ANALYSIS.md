# GitLab Duo Chat 协议逆向分析报告
=====================================

**分析日期**: 2026-06-17  
**分析目标**: https://gitlab.com/dashboard/home → GitLab Duo Chat  
**分析方法**: 浏览器 JS 注入拦截器 + 实时网络请求抓包  

---

## 一、架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                      浏览器前端                              │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────────┐    │
│  │ Chat UI  │──▶│ Apollo Client│──▶│ Network Layer    │    │
│  └──────────┘   └──────────────┘   │  ├─ fetch/GraphQL │    │
│                                   │  └─ ActionCable WS│    │
│                                   └────────┬───────────┘    │
└────────────────────────────────────────────┼───────────────┘
                                             │
                                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    GitLab 后端                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              POST /api/graphql                       │   │
│  │  ┌─────────────────────────────────────────────────┐ │   │
│  │  │  Duo Workflow System                            │ │   │
│  │  │  Ai::DuoWorkflows::Workflow                     │ │   │
│  │  │                                                │ │   │
│  │  │  用户发送消息 ──▶ 创建/更新 Workflow              │ │   │
│  │  │  AI 处理 ──▶ 更新 Checkpoint (消息列表)          │ │   │
│  │  │  前端轮询 ◀── getWorkflowLatestCheckpoint        │ │   │
│  │  └─────────────────────────────────────────────────┘ │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  POST /-/cable (WebSocket) — ActionCable 订阅               │
│  可能用于实时推送新消息（替代轮询）                           │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、已捕获的 API 调用详情

### 捕获 #1: 使用数据追踪（非核心）

```
POST /api/v4/usage_data/track_event
Status: 200
Content-Type: application/json
Body: (空)
```

> 这是 GitLab 的使用分析追踪，与聊天功能无关。

### 捕获 #2: 工作流检查点轮询 ⭐ 核心发现！

```http
POST /api/graphql
Content-Type: application/json; charset=utf-8
Accept: */*
X-Csrf-Token: Q7n_-AXEO2GuL5GM5uEg0M7raH2C-OLKGPvTsrBA5D8bSltde-yIQTCkcdpgfkmh2NNcsBO_dpqHuZmRJsm3Og
X-Gitlab-Feature-Category: duo_agent_platform
X-Gitlab-Version: 19.1.0-pre
```

**Request Body:**
```json
{
  "operationName": "getWorkflowLatestCheckpoint",
  "variables": {
    "workflowId": "gid://gitlab/Ai::DuoWorkflows::Workflow/4512521"
  },
  "query": "query getWorkflowLatestCheckpoint($workflowId: AiDuoWorkflowsWorkflowID!) {\n  duoWorkflowWorkflows(workflowId: $workflowId) {\n    nodes {\n      id\n      status\n      aiCatalogItemVersionId\n      workflowDefinition\n      archived\n      stalled\n      latestCheckpoint {\n        workflowGoal\n        workflowStatus\n        errors\n        duoMessages {\n          content\n          messageType\n          messageSubType\n          status\n          toolInfo\n          timestamp\n          correlationId\n          messageId\n          role\n          additionalContext {\n            category\n            id\n            content\n            metadata\n            __typename\n          }\n          __typename\n        }\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}"
}
```

**Response Body (关键数据结构):**
```json
{
  "data": {
    "duoWorkflowWorkflows": {
      "nodes": [
        {
          "id": "gid://gitlab/Ai::DuoWorkflows::Workflow/4512521",
          "status": "INPUT_REQUIRED",
          "aiCatalogItemVersionId": null,
          "workflowDefinition": "chat",
          "archived": false,
          "stalled": false,
          "latestCheckpoint": {
            "workflowGoal": "Hello, this is a test message. Please respond briefly.",
            "workflowStatus": "INPUT_REQUIRED",
            "errors": [],
            "duoMessages": [
              {
                "content": "Hello, this is a test message. Please respond briefly.",
                "messageType": "user",
                "messageSubType": null,
                "status": "success",
                "toolInfo": "null",
                "timestamp": "2026-06-17T05:00:17.799436+00:00",
                "correlationId": null,
                "messageId": "user-74f89723-a698-4bbe-8fba-873b40bb542c",
                "role": null,
                "additionalContext": [
                  {
                    "category": "REPOSITORY",
                    "id": "page-context",
                    "content": "<current_gitlab_page_url>https://gitlab.com/dashboard/home</current_gitlab_page_url>\n<current_gitlab_page_title>Home · GitLab</current_gitlab_page_title>",
                    "metadata": {
                      "icon": "link",
                      "title": "Current page",
                      "enabled": true,
                      "subType": "open_tab",
                      "pagePath": "/dashboard/home",
                      "projectPath": "",
                      "subTypeLabel": "Current page",
                      "secondaryText": "Page context /dashboard/home"
                    },
                    "__typename": "AiAdditionalContext"
                    }
                  ],
                  "__typename": "DuoMessage"
                },
                {
                  "content": "Hi! Got it, everything's working. How can I help you today?",
                  "messageType": "agent",
                  "messageSubType": null,
                  "status": "success",
                  "toolInfo": "null",
                  "timestamp": "2026-06-17T05:00:19.742249+00:00",
                  "correlationId": null,
                  "messageId": "lc_run--019ed3f3-9a1c-70a0-a108-9ab6a9dd45bb",
                  "role": null,
                  "additionalContext": null,
                  "__typename": "DuoMessage"
                }
              ],
              "__typename": "DuoWorkflowEvent"
            },
            "__typename": "DuoWorkflow"
          }
        ]
      ]
    }
  }
}
```

---

## 三、协议细节分析

### 3.1 认证机制

| Header | 值 | 说明 |
|--------|-----|------|
| `Cookie` | `_gitlab_session=...` | 主认证方式，Session Cookie |
| `X-Csrf-Token` | `<32位hex>` | CSRF 防护令牌，从页面 meta 标签获取 |
| `X-Gitlab-Feature-Category` | `duo_agent_platform` | 功能分类标识 |
| `X-Gitlab-Version` | `19.1.0-pre` | GitLab 版本号 |

**CSRF Token 获取方法:**
```html
<!-- 页面 HTML head 中 -->
<meta name="csrf-token" content="Q7n_-AXEO2GuL5GM5uEg0M7raH2C-OLKGPvTsrBA5D8bSltde-yIQTCkcdpgfkmh2NNcsBO_dpqHuZmRJsm3Og">
<meta name="csp-nonce" value="sWQ9/0s3HI6u3PoN5WRo9w==">
```

### 3.2 工作流生命周期

```
用户输入消息
     │
     ▼
┌─────────────────┐
│ INPUT_REQUIRED  │ ← 等待用户输入 (初始状态)
└────────┬────────┘
         │ 用户提交消息
         ▼
┌─────────────────┐
│ PROCESSING      │ ← AI 正在处理 (轮询中)
└────────┬────────┘
         │ AI 回复完成
         ▼
┌─────────────────┐
│ COMPLETED       │ ← 对话完成
└────────┬────────┘
         │ 用户继续输入
         ▼
┌─────────────────┐
│ INPUT_REQUIRED  │ ← 再次等待输入 (循环)
└─────────────────┘
```

### 3.3 消息类型

| messageType | 含义 |
|-------------|------|
| `user` | 用户发送的消息 |
| `agent` | AI 助手的回复 |

### 3.4 消息 ID 格式

- **用户消息**: `user-{uuid}` 例如 `user-74f89723-a698-4bbe-8fba-873b40bb542c`
- **AI 消息**: `lc_run--{uuid}` 例如 `lc_run--019ed3f3-9a1c-70a0-a108-9ab6a9dd45bb`
- **工作流 ID**: `gid://gitlab/Ai::DuoWorkflows::Workflow/{数字}`

### 3.5 附加上下文 (additionalContext)

AI 可以感知用户的当前页面环境：

```json
{
  "category": "REPOSITORY",
  "id": "page-context", 
  "content": "<current_gitlab_page_url>https://gitlab.com/dashboard/home</current_gitlab_page_url>\n<current_gitlab_page_title>Home · GitLab</current_gitlab_page_title>",
  "metadata": {
    "icon": "link",
    "title": "Current page",
    "enabled": true,
    "subType": "open_tab",
    "pagePath": "/dashboard/home"
  }
}
```

这解释了为什么 GitLab Duo 能回答关于当前项目/页面的上下文问题。

---

## 四、未捕获的部分（待进一步研究）

### 4.1 发送消息的 Mutation

⚠️ **重要**: 发送用户消息到工作流的 GraphQL mutation **未被拦截器捕获**。

可能的原因：
1. **ActionCable WebSocket**: GitLab 可能通过 `/-/cable` WebSocket 连接发送消息
2. **不同的 URL 模式**: 可能使用了非 graphql 的端点
3. **拦截器过滤条件**: 可能被关键词过滤器遗漏

**推测的候选 mutations** (基于 GitLab GraphQL schema 约定):
```graphql
# 候选 1: 发送聊天消息
mutation sendDuoChatMessage($input: AiDuoWorkflowsSendMessageInput!) {
  sendDuoChatMessage(input: $input) {
    errors
    workflow { id status }
  }
}

# 候选 2: 创建新工作流
mutation createDuoWorkflow($input: CreateDuoWorkflowInput!) {
  createDuoWorkflow(input: $input) {
    errors
    workflow { id status }
  }
}

# 候选 3: 提供用户输入 (响应 INPUT_REQUIRED 状态)
mutation submitWorkflowInput($input: SubmitWorkflowInputInput!) {
  submitWorkflowInput(input: $input) { ... }
}
```

### 4.2 ActionCable WebSocket 连接

从页面 HTML 中检测到：
```html
<meta name="csp-nonce" value="sWQ9/0s3HI6u3PoN5WRo9w==">
<!-- 用于 WebSocket 连接的 nonce -->
```

WebSocket 端点: `wss://gitlab.com/-/cable?...`

可能用于：
- 实时推送新的 checkpoint 更新
- 替代轮询机制
- 双向通信通道

### 4.3 流式输出机制

GitLab Duo 显示 "finding an answer..." 加载状态，然后一次性显示完整回复。
这可能意味着：
- 后端是流式生成的（SSE），但前端缓存后一次性渲染
- 或者后端生成完整回复后再写入 checkpoint

---

## 五、代理实现策略

基于以上分析，`server.py` v2 采用以下策略：

### 多策略消息发送
```
Strategy 1: sendDuoChatMessage mutation (首选)
    ↓ 失败
Strategy 2: aiAction mutation (备用)
    ↓ 失败  
Strategy 3: createDuoWorkflow mutation (兜底)
```

### 轮询式响应获取
```
发送消息 → 获得 workflow_id → 循环调用 getWorkflowLatestCheckpoint
                                    ↓
                             检测新 agent 类型消息
                                    ↓
                             转换为 OpenAI SSE 格式输出
```

### 终止条件检测
- `workflowStatus` 变为 COMPLETED/FINISHED/FAILED
- `node.status` 变为 COMPLETED/FINISHED/FAILED
- 超过最大轮询次数 (默认 180 次 × 1秒 = 3分钟)

---

## 六、安全与合规说明

⚠️ 本分析仅用于技术研究和学习目的。

1. 所有数据在本地浏览器中收集，未发送至任何第三方服务器
2. Cookie 和 Token 是敏感信息，请妥善保管
3. 请遵守 GitLab 服务条款，不要滥用 API
4. 此工具仅供个人学习和测试使用

---

*报告生成时间: 2026-06-17T05:10 UTC*
*分析工具: Tabbit Browser Automation + JS Interceptor*
