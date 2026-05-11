# s11: Gateway & API


> **"An agent is just a function with a REST API."**
> Harness layer: Service Integration

## Problem

Through s11, the agent runs as a terminal process. You have to SSH in, run `python chapters/11_cron.py`, and watch the logs. There is no way for external services to talk to the agent -- no HTTP endpoint, no API, no integration with other tools. The agent is a standalone binary, not a service.

This means:
- CI/CD pipelines cannot ask the agent for code review
- Web apps cannot integrate agent capabilities
- Other services have no way to send messages or query state
- There is no standard protocol for agent communication

## Solution

Wrap the agent loop in an **HTTP server** that exposes an OpenAI-compatible `/v1/chat/completions` endpoint. Now any tool that speaks OpenAI's API -- LangChain, AutoGPT, custom scripts, IDE plugins -- can talk to the agent. The agent becomes a service accessible over HTTP.

```
                           +-----------+
  Client apps -----------> |  Gateway  | ---> +-----------+
  (curl, Postman,          | (aiohttp) |      | AgentLoop |
   IDE plugins,            | :18790    |      |           |
   CI/CD pipelines)        |           |      | sessions  |
                           +-----------+      | tools     |
                                  |           | cron      |
                           +-----------+      +-----------+
                           | WebUI     |
                           | (React    |
                           |  SPA via  |
                           | websocket)|
                           +-----------+

HTTP API (OpenAI-compatible):

  POST /v1/chat/completions     -- send a message, get a response
  GET  /v1/models               -- list available models
  GET  /health                  -- health check

  Request:
  {
    "model": "openclaw-agent",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }

  Response:
  {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "created": 1715412345,
    "model": "openclaw-agent",
    "choices": [{
      "index": 0,
      "message": {"role": "assistant", "content": "Hi! How can I help?"},
      "finish_reason": "stop"
    }]
  }
```

The gateway bundles everything: the agent loop, session manager, message bus, channel manager, cron service, and a static WebUI. It is a single process you start via the gateway command.

## How It Works

1. **aiohttp server** creates an OpenAI-compatible API endpoint.

```python
def create_app(agent_loop, model_name="openclaw-agent", request_timeout=120.0):
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app["agent_loop"] = agent_loop
    app["model_name"] = model_name
    app["request_timeout"] = request_timeout
    app["session_locks"] = {}  # per-session serialization

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app
```

2. **Chat completions handler** translates OpenAI-format requests into agent turns.

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

    # Non-streaming path
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

3. **Streaming support** uses Server-Sent Events (SSE) to push tokens as they arrive.

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

The SSE format is identical to OpenAI's, so any streaming-compatible client works.

4. **Multipart upload support** for file-based interactions.

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

5. **Gateway startup** initializes all subsystems and starts the HTTP server.

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

    # Create the API app
    api_app = create_app(agent)

    # Start everything
    await asyncio.gather(
        agent.run(),
        channels.start_all(),
        cron.start(),
        web._run_app(api_app, host=config.api.host, port=config.api.port),
    )
```

The agent is now a full service: HTTP API, chat channels, cron scheduling, session persistence, all in one process.

## What Changed From s11

| Component | Before (s10) | After (s11) |
|-----------|-------------|-------------|
| Interface | Terminal / REPL | HTTP API + WebSocket + Channels |
| API protocol | None | OpenAI-compatible `/v1/chat/completions` |
| Streaming | In-bus only | SSE over HTTP |
| File upload | None | Multipart/form-data support |
| Model listing | None | `GET /v1/models` endpoint |
| Health check | None | `GET /health` |
| Client compatibility | CLI only | Any OpenAI-compatible client |
| External integration | None | CI/CD, web apps, IDE plugins |
| Web interface | None | Optional React SPA (WebUI) |
| Per-session locking | Agent-internal | HTTP-level `asyncio.Lock` per `session_key` |

## Try It

```bash
python chapters/12_gateway.py

# In another terminal:
curl http://localhost:8900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello!"}]}'

# Streaming:
curl http://localhost:8900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Count to 5"}], "stream": true}'

# Check available models:
curl http://localhost:8900/v1/models
```

Suggested prompts:
- `Start the gateway and send a curl request -- what does the response look like?`
- `Try streaming mode with curl -N -- what's different about the response format?`
- `Upload a file using multipart/form-data and ask the agent to analyze it`
- `What happens when two requests arrive for the same session at the same time?`

---

**Design Note:** In the reference implementation, the API server is a full OpenAI-compatible endpoint with streaming SSE, multipart file upload, per-session locking, empty-response retry, and base64 image handling. The gateway orchestrates the entire stack: `AgentLoop`, `MessageBus`, `ChannelManager`, `CronService`, `SessionManager`, heartbeat, and WebUI. The `process_direct()` method on `AgentLoop` handles the "agent as a function" pattern — just pass in text, get a response. This is the foundation for all non-channel integrations.

## Production Deployment Considerations

When deploying the gateway to production, consider:

| Concern | Recommendation |
|---------|---------------|
| **Concurrency** | Use a production ASGI server (uvicorn/gunicorn) instead of `http.server` |
| **Rate limiting** | Add per-user rate limits to prevent abuse |
| **Authentication** | Add API key validation via header or query parameter |
| **Session isolation** | Use per-session locks to prevent concurrent writes |
| **Health checks** | Add `/health` endpoint with dependency status |
| **Graceful shutdown** | Implement SIGTERM handler to drain active sessions |
| **Logging** | Structured JSON logging with request IDs |
| **Metrics** | Prometheus / OpenTelemetry for token usage, latency, errors |

The gateway in this session is intentionally minimal. Each production concern above maps to a focused enhancement that can be added without changing the agent loop.

---

**End of learn-openclaw tutorial.** You have built an AI agent from a 20-line `while True` loop to a full production service with multi-channel support, session persistence, cron scheduling, and an OpenAI-compatible API.