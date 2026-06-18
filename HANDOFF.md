# GitLab Duo Proxy — 项目移交文档

> 本文档供项目移交给 Trae（或其他 AI 助手/开发者）继续维护使用。
> 包含：项目全貌、架构、关键技术决策、当前状态、已知问题、部署方式、TODO。

---

## 一、项目概述

**GitLab Duo Proxy** 是一个将 GitLab Duo Chat 转换为 OpenAI 兼容 API 的代理服务。

- **GitHub 仓库**: https://github.com/djfuni/gitlab-duo-api
- **生产服务器**: `103.231.56.210:8088`（SSH root / 密码见原会话）
- **WebUI 地址**: http://103.231.56.210:8088/web
- **协议**: MIT

### 核心能力
- OpenAI 兼容 `/v1/chat/completions` + `/v1/responses`（Codex 可用）
- 多账号池（轮询/随机/最少使用，失败冷却切换）
- 浏览器辅助登录（Playwright 串流，自动抓 Cookie）
- API 密钥管理（`sk-` 格式）
- 多用户系统（注册/登录/JWT，邮箱验证码）
- Claude 风格 WebUI

---

## 二、技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.10+ / FastAPI / Uvicorn |
| 浏览器自动化 | Playwright (chromium headless) |
| HTTP 客户端 | httpx |
| 数据库 | SQLite (WAL 模式) |
| 邮件 | smtplib (SMTP SSL, 163.com) |
| 前端 | 原生 HTML + CSS + JS（单文件 `web/index.html`） |
| 部署 | systemd service |
| 进程管理 | 单进程，共享 Playwright Browser 实例 |

### 依赖（requirements.txt）
```
fastapi
uvicorn[standard]
httpx
pyyaml
playwright
```

---

## 三、项目结构

```
gitlab-duo-api/
├── server.py              # 主服务（FastAPI，~2000行）
├── browser_login.py       # Playwright 浏览器会话管理 + 聊天驱动
├── account_pool.py        # 旧版账号池（JSON 存储，已被 db.py 取代但仍在用）
├── api_keys.py            # 旧版 API 密钥管理（JSON 存储，已被 db.py 取代）
├── db.py                  # 新版多用户数据库（SQLite + JWT）
├── email_smtp.py          # 邮箱验证码发送
├── chat_driver.py         # 早期聊天驱动（部分已弃用，browser_login.py 内有新版）
├── config.yaml            # 配置文件
├── requirements.txt
├── web/
│   └── index.html         # 单文件前端（登录/注册/账号池/对话/密钥/设置）
├── research/              # 协议逆向分析工具（参考用）
│   ├── capture_api.html
│   ├── capture_network.js
│   └── read_captures.js
├── PROTOCOL_ANALYSIS.md   # 协议逆向分析报告
├── QUICKSTART.md
└── README.md
```

### 运行时数据（.gitignore 忽略）
```
data/duo.db               # SQLite 数据库（用户、账号、密钥）
accounts.json             # 旧版账号池存储
api_keys.json             # 旧版密钥存储
.commit_hash              # 当前 Git commit（用于更新检测）
```

---

## 四、核心架构

### 4.1 请求流程

```
客户端 (OpenWebUI/Codex/curl)
    │
    │  POST /v1/chat/completions  (Authorization: Bearer sk-xxx)
    ▼
server.py: chat_completions()
    │
    ├── 鉴权: 检查 sk- 密钥 (先查 api_keys.json 旧版，再查 SQLite 新版)
    │
    ├── 账号池选取: pool.acquire() → 返回一个 GitLab 账号
    │
    ├── 浏览器会话: login_mgr.get_pinned(account_id)
    │              或创建临时会话 (skip_nav=True, 注入 cookie)
    │
    ▼
browser_login.py: BrowserLoginSession.chat_stream()
    │
    ├── 1. 导航到 /dashboard/home (若不在)
    ├── 2. 点击 [data-testid="ai-chat-toggle"] 打开聊天面板
    ├── 3. 在 [data-testid="chat-prompt-input"] 输入 prompt
    ├── 4. 点击 [aria-label="Send chat message."] 发送
    ├── 5. 拦截 /api/graphql 响应，正则提取 workflow_id
    ├── 6. 用 httpx 轮询 getWorkflowLatestCheckpoint 查询
    └── 7. 流式 yield OpenAI SSE 格式
```

### 4.2 Playwright 共享架构（重要！）

**`BrowserLoginManager` 持有全局唯一的 Playwright 实例和 Browser**。
每个 `BrowserLoginSession` 只创建 Context（隔离 cookies），不创建新 Browser。

> ⚠️ 这是修复 chromium 进程泄漏的关键设计。之前的版本每个会话都 `async_playwright().start()`，
> 导致 41 个僵尸 chromium 进程吃掉 2GB 内存。**绝对不要改回每会话独立 Browser。**

### 4.3 数据存储（双轨制）

当前存在两套存储，**新版 SQLite 是主方向**：

| 数据 | 旧版 | 新版 | 状态 |
|------|------|------|------|
| 用户 | 无 | SQLite users 表 | ✅ 新版 |
| GitLab 账号 | accounts.json (AccountPool) | SQLite accounts 表 | ⚠️ 两套并存 |
| API 密钥 | api_keys.json (ApiKeyManager) | SQLite api_keys 表 | ⚠️ 两套并存 |

`chat_completions()` 里的账号选取逻辑目前仍走旧版 `pool.acquire()`。
**TODO: 统一迁移到 SQLite，移除 account_pool.py 和 api_keys.py。**

---

## 五、API 端点清单

### 用户认证
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/auth/send-code` | 发送邮箱验证码 `{email}` |
| POST | `/v1/auth/register` | 注册 `{email, code, username, password}` |
| POST | `/v1/auth/login` | 登录 `{username, password}` → JWT |
| GET | `/v1/auth/me` | 当前用户信息 (Bearer JWT) |

### 用户资源（需 Bearer JWT）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/user/accounts` | 列出我的 GitLab 账号 |
| POST | `/v1/user/accounts` | 添加账号 |
| PUT | `/v1/user/accounts/{id}` | 更新账号 |
| DELETE | `/v1/user/accounts/{id}` | 删除账号 |
| GET | `/v1/user/api-keys` | 列出我的密钥 |
| POST | `/v1/user/api-keys` | 生成密钥（返回原始 key 仅一次） |
| DELETE | `/v1/user/api-keys/{id}` | 吊销密钥 |

### OpenAI 兼容（需 Bearer sk-xxx 或匿名）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/chat/completions` | Chat Completions（流式/非流式） |
| POST | `/v1/responses` | Responses API（Codex 兼容） |

### 系统（需 X-WebUI-Token）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/system/update/check` | 检查 GitHub 更新 |
| POST | `/v1/system/update/do` | git pull + restart |
| GET | `/v1/accounts/info` | 服务信息 |
| WS | `/v1/accounts/pool/assist/ws` | 浏览器登录串流 |

### WebUI
| 路径 | 说明 |
|------|------|
| `GET /web` | 主界面 |

---

## 六、关键技术决策与坑点

### 6.1 Duo Chat 页面结构（2026-06 实测）
```
聊天开关: [data-testid="ai-chat-toggle"]
输入框:   [data-testid="chat-prompt-input"]  (textarea)
发送按钮: [aria-label="Send chat message."]
页面:     /dashboard/home (不是 /-/duo_chat，后者 404)
```
> GitLab 前端会变，如果聊天失效，第一步用 Playwright dump 页面元素重新抓选择器。

### 6.2 Workflow 状态判断
GitLab Duo 的 workflow 完成后状态是 `INPUT_REQUIRED`（等待下次输入），**不是** `COMPLETED`。
```python
# 正确的终态判断
if status in ("COMPLETED", "FINISHED", "FAILED", "ERROR") or \
   (got_agent and status == "INPUT_REQUIRED"):
    # 结束
```

### 6.3 Cloudflare 绕过
- 浏览器登录：先访问 `/dashboard/home`（会 302 到登录页），等 CF 挑战自动清除（最多 15s），再让用户操作
- 聊天请求：httpx 轮询 `/api/graphql` 时带上浏览器里的 `cf_clearance` cookie
- **不要**用 httpx 直接 POST 登录表单（会触发 429 限流）

### 6.4 已删除的功能
- **真实浏览器登录代理**（`/auth/proxy/*`）已删除，因为 iframe + `<base>` 标签导致鉴权复杂化且不稳定

### 6.5 前端 JS 注意事项
- `web/index.html` 是单文件，`<script>` 块内的 JS **大括号必须平衡**，否则所有函数失效
- 修改后务必用 brace count 检查：`python -c "..."`（见历史修复记录）
- Clipboard API 在 HTTP 下为 `undefined`，需 `execCommand` 回退

### 6.6 SQLite 兼容性
服务器 SQLite 版本较老，不支持 `DEFAULT (unixepoch())`。
**所有时间戳用 Python `time.time()` 传入，不要用 SQL 函数默认值。**

---

## 七、部署方式

### 服务器信息
- IP: `103.231.56.210`
- 端口: `8088`
- 项目目录: `/opt/gitlab-duo-api`
- Python venv: `/opt/gitlab-duo-api/venv`
- systemd 服务: `gitlab-duo-api`

### 常用命令
```bash
# SSH 连接
ssh root@103.231.56.210

# 查看服务状态
systemctl status gitlab-duo-api

# 重启服务
systemctl restart gitlab-duo-api

# 查看日志
journalctl -u gitlab-duo-api -f

# 更新代码
cd /opt/gitlab-duo-api && git pull origin main && systemctl restart gitlab-duo-api

# 查看 chromium 进程数（正常 6 个左右，若 >15 说明有泄漏）
ps aux | grep chrome-headless | grep -v grep | wc -l
```

### systemd 配置
```ini
# /etc/systemd/system/gitlab-duo-api.service
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
```

### 配置文件
编辑 `/opt/gitlab-duo-api/config.yaml`：
- `server.port`: 8088
- `pool.webui_token`: `duo-admin-2026`（系统级管理令牌）
- `models`: 模型映射表

### 邮箱配置
`email_smtp.py` 内硬编码：
```python
SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465
SMTP_USER = "agcwhml2025@163.com"
SMTP_PASS = "SGpCG4bFp7VwKBaA"
```
> 建议后续迁移到 config.yaml 或环境变量

---

## 八、当前状态

### 已完成
- [x] OpenAI 兼容 `/v1/chat/completions`
- [x] OpenAI Responses API `/v1/responses`（Codex 兼容）
- [x] 多账号池 + 浏览器辅助登录
- [x] API 密钥管理
- [x] 多用户注册/登录（JWT + 邮箱验证码）
- [x] WebUI 登录/注册/账号管理/密钥管理/对话测试
- [x] GitHub 一键更新检测
- [x] Playwright 共享 Browser（修复进程泄漏）
- [x] 聊天响应速度优化（pinned session 复用）
- [x] 轮询超时修复（INPUT_REQUIRED 状态）

### 已知问题 / TODO
- [ ] **数据层统一**：account_pool.py 和 api_keys.py 旧版存储仍在使用，需迁移到 SQLite
- [ ] **chat_completions 账号选取**：目前走旧版 `pool.acquire()`，应改为从 SQLite 读取用户账号
- [ ] **用户级账号池调度**：当前所有用户的账号混在全局池里，应按 user_id 隔离
- [ ] **邮箱配置外置**：email_smtp.py 硬编码了 SMTP 凭据，应迁移到 config.yaml
- [ ] **前端优化**：设置页已简化但仍残留旧字段引用
- [ ] **chat_driver.py 清理**：部分功能已被 browser_login.py 取代，可删除
- [ ] **HTTPS**：当前是 HTTP，Clipboard API 和安全性需要 HTTPS（可用 Caddy 反代）
- [ ] **API 密钥与用户绑定**：当前 sk- 密钥鉴权后走全局池，应绑定到用户再查用户的账号

---

## 九、Git 提交历史要点

| Commit | 内容 |
|--------|------|
| 初始提交 | 17 文件：核心服务 + WebUI + 协议分析 |
| `fix: clipboard` | HTTP 下 `navigator.clipboard` 为 undefined 的回退 |
| `fix: chromium leak` | 共享 Playwright Browser，修复 41 个僵尸进程 |
| `fix: CF pre-warm` | 登录前先过 Cloudflare 挑战 |
| `feat: httpx login` | httpx 直接 POST 登录（后被 429，已废弃） |
| `feat: proxy auth` | 反向代理登录（已删除，不稳定） |
| `fix: polling timeout` | INPUT_REQUIRED 状态识别 |
| `feat: Responses API` | `/v1/responses` Codex 兼容 |
| `feat: multi-tenant` | SQLite 多用户系统 |
| `feat: email verify` | 163 邮箱验证码注册 |
| `fix: JS brace` | 大括号不平衡导致所有按钮失效 |

---

## 十、给 Trae 的建议

1. **先跑起来**：`ssh root@103.231.56.210` → `systemctl status gitlab-duo-api` 确认服务正常 → 访问 `http://103.231.56.210:8088/web`

2. **改前端时**：每次改完 `web/index.html` 的 `<script>` 块，务必检查大括号平衡，否则所有 JS 函数静默失效。

3. **改聊天逻辑时**：GitLab 前端会变。先用 Playwright 在服务器上 dump 页面元素，确认选择器没变再改代码。

4. **改数据层时**：优先统一到 `db.py` (SQLite)，逐步淘汰 `account_pool.py` 和 `api_keys.py`。

5. **部署方式**：本地改完代码 → SFTP 上传到 `/opt/gitlab-duo-api/` → `systemctl restart gitlab-duo-api`。或用 WebUI 的「检查更新」一键 git pull + restart。

6. **GitHub push**：若遇到 SSL 证书吊销检查错误，用 `GIT_SSL_NO_VERIFY=1 git push origin main`。

7. **测试账号**：服务器上已有测试用户 `test` / `123456`，可直接登录 WebUI 测试。

---

## 十一、关键文件快速索引

| 要做什么 | 看哪个文件 |
|---------|-----------|
| 加新 API 端点 | `server.py` |
| 改聊天逻辑 | `browser_login.py` → `chat_stream()` |
| 改登录流程 | `browser_login.py` → `start()` + `login_via_httpx()` |
| 改数据库结构 | `db.py` → `SCHEMA` |
| 改前端界面 | `web/index.html` |
| 改模型列表 | `config.yaml` → `models` |
| 改邮箱配置 | `email_smtp.py` |
| 理解协议 | `PROTOCOL_ANALYSIS.md` |

---

*文档生成时间: 2026-06-18*
*项目仓库: https://github.com/djfuni/gitlab-duo-api*
