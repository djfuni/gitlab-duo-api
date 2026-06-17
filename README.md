# GitLab Duo Chat → OpenAI API Proxy

将 GitLab Duo Chat 转换为 OpenAI 兼容 API，支持多账号池、浏览器辅助登录、流式响应。

## 特性

- **OpenAI 兼容** — `/v1/chat/completions` + `/v1/models`，可直接接入 OpenWebUI、ChatBox 等客户端
- **账号池** — 多账号轮询/随机/最少使用调度，失败自动冷却切换
- **浏览器辅助登录** — WebUI 内置 Playwright 浏览器串流，登录 GitLab 后自动抓取 Cookie
- **API 密钥** — 生成 `sk-` 格式密钥，方便第三方客户端接入
- **流式响应** — SSE 格式，完整兼容 OpenAI SDK
- **Claude 风格 WebUI** — 暖白底+珊瑚橙，账号管理、对话测试、密钥管理一站式
- **多模型** — Claude Opus 4.8 / Sonnet 4 / Haiku 3.5 / GPT-5.5 等

## 快速开始

### 部署到服务器 (推荐)

```bash
# 安装依赖
apt update && apt install -y python3 python3-pip python3-venv

# 克隆项目
git clone https://github.com/djfuni/gitlab-duo-api.git
cd gitlab-duo-api

# 安装 Python 依赖
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 安装 Playwright 浏览器 (浏览器辅助登录功能需要)
venv/bin/playwright install chromium
venv/bin/playwright install-deps chromium

# 启动 (默认 8080 端口)
GITLAB_PROXY_PORT=8088 venv/bin/python server.py
```

### systemd 自启动

```bash
cat > /etc/systemd/system/gitlab-duo-api.service <<'EOF'
[Unit]
Description=GitLab Duo API Proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/gitlab-duo-api
ExecStart=/opt/gitlab-duo-api/venv/bin/python /opt/gitlab-duo-api/server.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now gitlab-duo-api
```

### 配置

编辑 `config.yaml`：

```yaml
server:
  port: 8088

pool:
  enabled: true
  strategy: round_robin
  webui_token: "your-admin-token"

models:
  claude-opus-4.8:
    id: "anthropic/claude-opus-4.8"
    provider: "anthropic"
  # ... 更多模型
```

## 使用

### 1. 添加 GitLab 账号

打开 `http://your-server:8088/web`，输入 WebUI 令牌登录：

- **浏览器登录**（推荐）— 内置 Playwright 浏览器，直接登录 GitLab 自动抓 Cookie
- **手动添加** — 从浏览器 F12 复制 Cookie 粘贴

### 2. 生成 API 密钥

WebUI → 「API 密钥」→ 生成 → 复制密钥。

### 3. 接入客户端

```bash
# curl
curl http://your-server:8088/v1/chat/completions \
  -H "Authorization: Bearer sk-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4.8","messages":[{"role":"user","content":"hello"}]}'

# OpenAI SDK
from openai import OpenAI
client = OpenAI(base_url="http://your-server:8088/v1", api_key="sk-xxxxxxxx")
client.chat.completions.create(model="claude-opus-4.8", messages=[{"role":"user","content":"hello"}])
```

### 4. 对话测试

WebUI → 「对话测试」→ 选择模型 → 发消息，流式输出。

## 技术架构

```
┌──────────────┐     ┌──────────────────┐     ┌──────────┐
│  OpenWebUI   │────▶│  Duo API Proxy   │────▶│  GitLab   │
│  ChatBox     │     │  /v1/chat        │     │  Duo Chat │
│  OpenAI SDK  │     │                  │     │           │
└──────────────┘     │  ┌─────────────┐ │     │  Workflow │
                     │  │ Account Pool│ │     │  System   │
                     │  │ Browser Chat│ │     │           │
                     │  └─────────────┘ │     └──────────┘
                     └──────────────────┘
```

- **发送**：Playwright 驱动真实 GitLab Duo Chat UI 发送消息，拦截 GraphQL 响应获取 `workflow_id`
- **接收**：用抓包验证过的 `getWorkflowLatestCheckpoint` 查询轮询回复
- **Cloudflare 绕过**：浏览器辅助登录产生的 Cookie 含 `cf_clearance`，后续请求复用相同的浏览器指纹自动通过

## 协议说明

本项目基于对 gitlab.com Duo Chat 的协议逆向分析。详细分析报告见 [PROTOCOL_ANALYSIS.md](PROTOCOL_ANALYSIS.md)。

关键发现：
- **查询端点**：`POST /api/graphql` → `getWorkflowLatestCheckpoint` 查询（已验证可用）
- **发送方式**：由于发送 mutation 未被拦截器捕获（见报告 4.1 节），采用 Playwright 驱动真实 UI 发送
- **认证**：`_gitlab_session` Cookie + `X-Csrf-Token` 请求头

## License

MIT
