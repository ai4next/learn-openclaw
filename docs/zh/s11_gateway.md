# s11: 网关与 API


> **"代理本质上就是一个带有 REST API 的函数。"**
> 接入层：服务集成

## 问题

在 s10 阶段，代理是以终端进程的方式运行的。你需要通过 SSH 登录，运行 `python chapters/11_cron.py`，然后查看日志。外部服务无法与代理通信——没有 HTTP 端点，没有 API，也没有与其他工具的集成。代理是一个独立的二进制程序，而不是一个服务。

这意味着：
- CI/CD 流水线无法请求代理进行代码审查
- Web 应用无法集成代理能力
- 其他服务无法发送消息或查询状态
- 没有标准的代理通信协议

## 解决方案

将代理循环封装在一个 **HTTP 服务器**中，该服务器暴露一个兼容 OpenAI 的 `/v1/chat/completions` 端点。现在，任何使用 OpenAI API 的工具——LangChain、AutoGPT、自定义脚本、IDE 插件——都可以与代理通信。代理变成了一个可通过 HTTP 访问的服务。

```
                           +-----------+
  Client apps -----------> |  网关     | ---> +-----------+
  (curl, Postman,          | (aiohttp) |      | AgentLoop |
  IDE 插件,                | :18790    |      |           |
  CI/CD 流水线)            |           |      | sessions  |
                           +-----------+      | tools     |
                                  |           | cron      |
                           +-----------+      +-----------+
                           | WebUI     |
                           | (React    |
                           |  SPA 通过 |
                           | websocket)|
                           +-----------+

HTTP API（兼容 OpenAI）：

  POST /v1/chat/completions     -- 发送消息，获取响应
  GET  /v1/models               -- 列出可用模型
  GET  /health                  -- 健康检查

  请求：
  {
    "model": "openclaw-agent",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": false
  }

  响应：
  {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "created": 1715412345,
    "model": "openclaw-agent",
    "choices": [{
      "index": 0,
      "message": {"role": "assistant", "content": "嗨！有什么可以帮你的？"},
      "finish_reason": "stop"
    }]
  }
```

网关集成了所有组件：代理循环、会话管理器、消息总线、频道管理器、定时任务服务以及静态 WebUI。它是一个单一的进程，通过网关命令启动。

## 工作原理

1. **aiohttp 服务器**创建一个兼容 OpenAI 的 API 端点。

```python
def create_app(agent_loop, model_name="openclaw-agent", request_timeout=120.0):
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app["agent_loop"] = agent_loop
    app["model_name"] = model_name
    app["request_timeout"] = request_timeout
    app["session_locks"] = {}  # 每个会话的序列化锁

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app
```

2. **聊天补全处理器**将 OpenAI 格式的请求转换为代理的对话轮次。

```python
async def handle_chat_completions(request: web.Request) -> web.Response:
    agent_loop = request.app["agent_loop"]
    timeout_s = request.app.get("request_timeout", 120.0)

    body = await request.json()
    stream = body.get("stream", False)
    text, media_paths = _parse_json_content(body)
    session_key = f"api:{body.get('session_id', 'default')}"

    if stream:
        return await _handle_streaming(agent_loop, text, media_paths,
                                       session_key, timeout_s)

    # 非流式路径
    async with session_lock:
        response = await asyncio.wait_for(
            agent_loop.process_direct(
                content=text,
                media=media_paths or None,
                session_key=session_key,
                channel="api",
                chat_id="default",
            ),
            timeout=timeout_s,
        )
        response_text = _response_text(response)

    return web.json_response(_chat_completion_response(
        response_text, model_name
    ))
```

3. **流式支持**使用服务器发送事件（SSE）在令牌生成时实时推送。

```python
async def _handle_streaming(agent_loop, text, media, session_key, timeout):
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    await resp.prepare(request)

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def on_stream(token: str) -> None:
        if token:
            await queue.put(token)

    async def _run():
        await agent_loop.process_direct(
            content=text, media=media,
            session_key=session_key,
            channel="api", chat_id="default",
            on_stream=on_stream,
        )
        await queue.put(None)

    task = asyncio.create_task(_run())
    while True:
        token = await queue.get()
        if token is None:
            break
        await resp.write(_sse_chunk(token, model_name, chunk_id))

    await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
    await resp.write(b"data: [DONE]\n\n")
    return resp
```

SSE 格式与 OpenAI 完全一致，因此任何支持流式传输的客户端都可以直接使用。

4. **多部分上传支持**用于基于文件的交互。

```python
async def _parse_multipart(request):
    reader = await request.multipart()
    text = ""
    session_id = None
    media_paths = []

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "message":
            text = (await part.read()).decode("utf-8")
        elif part.name == "session_id":
            session_id = (await part.read()).decode("utf-8").strip()
        elif part.name == "files":
            raw = await part.read()
            filename = f"{uuid.uuid4().hex}_{safe_filename(part.filename)}"
            dest = media_dir / filename
            dest.write_bytes(raw)
            media_paths.append(str(dest))

    return text, media_paths, session_id, model
```

5. **网关启动**初始化所有子系统并启动 HTTP 服务器。

```python
async def run_gateway(config_path: Path):
    config = load_config(config_path)
    config = resolve_config_env_vars(config)

    bus = MessageBus()
    agent = AgentLoop.from_config(config, bus=bus)
    session_manager = agent.sessions
    cron = CronService(store_path=config.workspace_path / "cron" / "jobs.json",
                       on_job=agent.process_cron_job)
    channels = ChannelManager(config, bus, session_manager=session_manager)

    # 创建 API 应用
    api_app = create_app(agent)

    # 启动所有组件
    await asyncio.gather(
        agent.run(),
        channels.start_all(),
        cron.start(),
        web._run_app(api_app, host=config.api.host, port=config.api.port),
    )
```

至此，代理成为一个完整的服务：HTTP API、聊天频道、定时任务调度、会话持久化，全部集成在一个进程中。

## 从 s10 到 s11 的变更

| 组件 | 之前 (s10) | 之后 (s11) |
|-----------|-------------|-------------|
| 交互界面 | 终端 / REPL | HTTP API + WebSocket + Channels |
| API 协议 | 无 | 兼容 OpenAI 的 `/v1/chat/completions` |
| 流式传输 | 仅在总线内 | 基于 SSE 的 HTTP |
| 文件上传 | 无 | 支持 Multipart/form-data |
| 模型列表 | 无 | `GET /v1/models` 端点 |
| 健康检查 | 无 | `GET /health` |
| 客户端兼容性 | 仅 CLI | 任何兼容 OpenAI 的客户端 |
| 外部集成 | 无 | CI/CD、Web 应用、IDE 插件 |
| Web 界面 | 无 | 可选的 React SPA (WebUI) |
| 每个会话的锁定 | 代理内部 | HTTP 层按 `session_key` 的 `asyncio.Lock` |

## 试试看

```bash
python chapters/12_gateway.py

# 在另一个终端中：
curl http://localhost:8900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好！"}]}'

# 流式传输：
curl http://localhost:8900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"从1数到5"}], "stream": true}'

# 查看可用模型：
curl http://localhost:8900/v1/models
```

建议尝试的提示词：
- `启动网关并发送一个 curl 请求——响应看起来是什么样的？`
- `尝试使用 curl -N 的流式模式——响应格式有什么不同？`
- `使用 multipart/form-data 上传一个文件，让代理分析它`
- `当两个请求同时到达同一个会话时会发生什么？`

---

**设计说明：** 在参考实现中，API 服务器是一个完整的兼容 OpenAI 的端点，支持流式 SSE、多部分文件上传、每会话锁定、空响应重试以及 base64 图片处理。网关编排了整个技术栈：`AgentLoop`、`MessageBus`、`ChannelManager`、`CronService`、`SessionManager`、心跳检测以及 WebUI。`AgentLoop` 上的 `process_direct()` 方法实现了"代理即函数"的模式——只需传入文本即可获得响应。这是所有非频道集成的基础。

## 生产部署注意事项

将网关部署到生产环境时，请考虑：

| 关注点 | 建议 |
|---------|---------------|
| **并发** | 使用生产环境的 ASGI 服务器（uvicorn/gunicorn）替代 `http.server` |
| **速率限制** | 添加每用户的速率限制以防止滥用 |
| **身份认证** | 通过请求头或查询参数添加 API 密钥验证 |
| **会话隔离** | 使用每个会话的锁防止并发写入 |
| **健康检查** | 添加包含依赖状态的 `/health` 端点 |
| **优雅关闭** | 实现 SIGTERM 处理器以排空活跃会话 |
| **日志记录** | 带有请求 ID 的结构化 JSON 日志 |
| **指标监控** | 使用 Prometheus / OpenTelemetry 监控令牌用量、延迟和错误 |

本会话中的网关有意保持最小化。上述每一个生产环境的关注点都对应一个可以在不修改代理循环的前提下添加的针对性增强功能。

---

**learn-openclaw 教程到此结束。** 你已经将一个仅 20 行的 `while True` 循环构建成了一个完整的 AI 代理生产级服务，具备多频道支持、会话持久化、定时任务调度以及兼容 OpenAI 的 API。