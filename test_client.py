#!/usr/bin/env python3
"""
GitLab Duo Chat → OpenAI API Proxy 测试客户端
==============================================

演示如何使用 OpenAI SDK 调用 GitLab Duo Chat 代理。

使用方式：
    # 1. 先启动代理服务
    python server.py
    
    # 2. 运行测试客户端
    python test_client.py
    
    # 或直接用 curl:
    # 见下方 curl 示例
"""

import json
import sys
import time
import os


# ============================================================
# 方式 1: 使用 OpenAI SDK (推荐)
# ============================================================

def test_with_openai_sdk():
    """使用官方 OpenAI SDK 调用代理"""
    try:
        from openai import OpenAI
    except ImportError:
        print("⚠️  需要安装 openai: pip install openai")
        return False
    
    # 从环境变量或配置读取
    base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "gitlab-proxy-key")  # 可为任意值，或填入实际cookie/token
    
    print(f"🔗 连接到: {base_url}")
    
    client = OpenAI(base_url=base_url, api_key=api_key)
    
    # ---- 测试 1: 列出模型 ----
    print("\n" + "=" * 50)
    print("📋 测试 1: 列出可用模型")
    print("=" * 50)
    try:
        models = client.models.list()
        for m in models.data:
            print(f"  ✅ {m.id} (owned_by: {m.owned_by})")
    except Exception as e:
        print(f"  ❌ 失败: {e}")
    
    # ---- 测试 2: 非流式对话 ----
    print("\n" + "=" * 50)
    print("💬 测试 2: 非流式对话")
    print("=" * 50)
    try:
        response = client.chat.completions.create(
            model="claude-opus-4.8",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is 2+2? Reply with just the number."},
            ],
            stream=False,
        )
        
        msg = response.choices[0].message
        print(f"  🤖 回复: {msg.content}")
        print(f"  📊 Token 使用: prompt={response.usage.prompt_tokens}, completion={response.usage.completion_tokens}")
        print(f"  🔑 完成原因: {response.choices[0].finish_reason}")
        print(f"  📝 模型: {response.model}")
    except Exception as e:
        print(f"  ❌ 失败: {e}")
    
    # ---- 测试 3: 流式对话 ----
    print("\n" + "=" * 50)
    print("🌊 测试 3: 流式对话 (SSE)")
    print("=" * 50)
    try:
        stream = client.chat.completions.create(
            model="claude-opus-4.8",
            messages=[
                {"role": "user", "content": "Count from 1 to 5, one per line."},
            ],
            stream=True,
        )
        
        print("  📡 流式输出:")
        full_content = []
        for chunk in stream:
            delta = chunk.choices[0].delta
            content = delta.content or ""
            if content:
                print(f"     {content}", end="", flush=True)
                full_content.append(content)
        
        print(f"\n\n  ✅ 完成! 总长度: {len(''.join(full_content))} 字符")
    except Exception as e:
        print(f"  ❌ 失败: {e}")
    
    return True


# ============================================================
# 方式 2: 使用 httpx / requests (原生 HTTP)
# ============================================================

def test_with_httpx():
    """使用原生 HTTP 调用（无需 openai SDK）"""
    try:
        import httpx
    except ImportError:
        print("⚠️  需要安装 httpx: pip install httpx")
        return False
    
    base_url = os.environ.get("PROXY_URL", "http://localhost:8080").rstrip("/")
    
    print(f"\n{'=' * 50}")
    print(f"🔧 原生 HTTP 测试 ({base_url})")
    print("=" * 50)
    
    # 流式请求示例
    print("\n🌊 流式请求:")
    with httpx.stream(
        "POST",
        f"{base_url}/v1/chat/completions",
        json={
            "model": "claude-opus-4.8",
            "messages": [{"role": "user", "content": "Say hello in Chinese."}],
            "stream": True,
        },
        timeout=120,
    ) as resp:
        print(f"  Status: {resp.status_code}")
        for line in resp.iter_lines():
            if line.startswith("data: ") and "[DONE]" not in line:
                data = json.loads(line[6:])
                content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if content:
                    print(f"     {content}", end="", flush=True)
            elif "[DONE]" in line:
                print("\n  ✅ [DONE]")
    
    return True


# ============================================================
# 方式 3: curl 命令参考
# ============================================================

def print_curl_examples():
    """打印可直接使用的 curl 命令"""
    proxy_url = os.environ.get("PROXY_URL", "http://localhost:8080")
    
    print("\n" + "=" * 60)
    print("📌 curl 命令参考")
    print("=" * 60)
    
    print("""
# ===== 1. 列出模型 =====
curl {proxy_url}/v1/models \\
  -H "Authorization: Bearer any-token"

# ===== 2. 非流式对话 =====
curl {proxy_url}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer your-cookie-or-token" \\
  -d '{{
    "model": "claude-opus-4.8",
    "messages": [{{"role": "user", "content": "Hello!"}}],
    "stream": false
  }}'

# ===== 3. 流式对话 (SSE) =====
curl {proxy_url}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer your-cookie-or-token" \\
  -d '{{
    "model": "claude-opus-4.8",
    "messages": [{{"role": "user", "content": "Write a haiku."}}],
    "stream": true
  }}'

# ===== 4. 切换账号 (运行时) =====
curl {proxy_url}/v1/accounts/switch \\
  -H "Content-Type: application/json" \\
  -d '{{"auth_type": "cookie", "auth_value": "_gitlab_session=NEW_SESSION"}}'

# ===== 5. 查看当前账号信息 =====
curl {proxy_url}/v1/accounts/info \\
  -H "Authorization: Bearer any-token"

# ===== 6. 健康检查 =====
curl {proxy_url}/health
""".format(proxy_url=proxy_url))


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║  GitLab Duo Chat → OpenAI API 测试客户端   ║")
    print("╚══════════════════════════════════════════╝")
    
    # Print curl examples always
    print_curl_examples()
    
    # Run tests based on args
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    
    if mode in ("all", "sdk"):
        test_with_openai_sdk()
    
    if mode in ("all", "http"):
        test_with_httpx()
    
    print("\n✨ 所有测试完成!")
