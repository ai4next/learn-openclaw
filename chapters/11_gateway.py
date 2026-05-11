#!/usr/bin/env python3
"""Harness layer: Gateway & API

    +-----------+
    | HTTP      |  /v1/chat/completions
    | Server    |  GET /health
    +-----------+  POST /messages
         |
    +-----------+
    | Agent     |  Process API requests
    | Runner    |
    +-----------+

Key insight: An agent is just a function with a REST API.
The /v1/chat/completions endpoint makes any agent
a drop-in replacement for any OpenAI-compatible client.
"""

import http.server
import json
import os
import threading
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Gateway Config ──────────────────────────────────────────────────────

@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    model: str = field(default_factory=lambda: os.getenv("MODEL_ID", "claude-sonnet-4-6"))


# ── Tools ───────────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self):
        self._tools = {}
    def register(self, tool):
        self._tools[tool.name] = tool
    def list_schemas(self):
        return [t.to_api_schema() for t in self._tools.values()]
    def execute(self, name, **kwargs):
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        return tool.execute(**kwargs)

class Tool:
    @property
    def name(self): raise NotImplementedError
    @property
    def description(self): raise NotImplementedError
    @property
    def parameters(self): raise NotImplementedError
    def to_api_schema(self):
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}
    def execute(self, **kwargs): raise NotImplementedError

class BashTool(Tool):
    name = "bash"
    description = "Execute a shell command"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    def execute(self, command=""):
        try:
            r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            out = r.stdout or ""
            if r.stderr: out += f"\nSTDERR:\n{r.stderr}"
            if r.returncode != 0: out += f"\nExit code: {r.returncode}"
            return out or "(no output)"
        except Exception as e: return f"Error: {e}"

class ReadTool(Tool):
    name = "read"
    description = "Read a file"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    def execute(self, file_path=""):
        try: return Path(file_path).resolve().read_text(encoding="utf-8")
        except Exception as e: return f"Error: {e}"

class WriteTool(Tool):
    name = "write"
    description = "Write content to a file"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}
    def execute(self, file_path="", content=""):
        try:
            p = Path(file_path).resolve(); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8"); return f"Written {len(content)} bytes"
        except Exception as e: return f"Error: {e}"


# ── Gateway ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = "You are an OpenClaw agent running as an API service. You have bash, read, and write tools."

registry = ToolRegistry()
for t in [BashTool(), ReadTool(), WriteTool()]:
    registry.register(t)


class Gateway:
    """HTTP gateway that exposes the agent as an OpenAI-compatible API."""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the HTTP server in a background thread."""
        handler = self._make_handler()

        self._server = http.server.HTTPServer(
            (self.config.host, self.config.port),
            handler,
        )

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        url = f"http://{self.config.host}:{self.config.port}"
        print(f"  Gateway started at {url}")
        print(f"  GET  {url}/health")
        print(f"  POST {url}/v1/chat/completions")

    def stop(self):
        if self._server:
            self._server.shutdown()
            print("  Gateway stopped")

    def _make_handler(self):
        """Create a request handler class bound to this gateway."""
        gateway = self

        class GatewayHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                """Suppress default logging; we print our own."""
                pass

            def _send_json(self, data, status=200):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self):
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 0:
                    return json.loads(self.rfile.read(content_length))
                return {}

            def do_GET(self):
                if self.path == "/health":
                    self._send_json({
                        "status": "ok",
                        "model": gateway.config.model,
                        "uptime": time.time() - gateway._start_time,
                    })
                else:
                    self._send_json({"error": "not found"}, 404)

            def do_POST(self):
                if self.path == "/v1/chat/completions":
                    self._handle_chat_completion()
                else:
                    self._send_json({"error": "not found"}, 404)

            def _handle_chat_completion(self):
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "invalid JSON body"}, 400)
                    return

                model = body.get("model", gateway.config.model)
                messages_in = body.get("messages", [])
                max_tokens = body.get("max_tokens", 4096)
                stream = body.get("stream", False)

                if not messages_in:
                    self._send_json({"error": "messages is required"}, 400)
                    return

                if stream:
                    self._send_json({"error": "streaming not supported"}, 501)
                    return

                # Convert OpenAI-style messages to Anthropic format
                msgs = []
                for m in messages_in:
                    role = m.get("role", "user")
                    content = m.get("content", "")
                    if role == "system":
                        continue  # skip, system is handled separately
                    msgs.append({"role": role, "content": content})

                # Extract system prompt from messages
                system_text = SYSTEM_PROMPT
                for m in messages_in:
                    if m.get("role") == "system":
                        system_text = m.get("content", SYSTEM_PROMPT)

                # Run the agent loop inline
                try:
                    content_blocks = gateway._run_agent(msgs, model, system_text, max_tokens)
                except Exception as e:
                    self._send_json({
                        "error": f"agent error: {e}",
                    }, 502)
                    return

                # Build OpenAI-compatible response
                response_text = ""
                for block in content_blocks:
                    if getattr(block, "type", None) == "text":
                        response_text += getattr(block, "text", str(block))
                    elif isinstance(block, dict) and block.get("type") == "text":
                        response_text += block.get("text", "")

                response = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_text,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }

                self._send_json(response)

        return GatewayHandler

    def _run_agent(self, messages, model, system_prompt, max_tokens):
        """Run the agent loop for a single turn (no tool loops for simplicity)."""
        response = client.messages.create(
            model=model,
            system=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
            tools=registry.list_schemas(),
        )

        # Handle tool calls in a simple loop (max 5 rounds)
        current_messages = list(messages)
        current_messages.append({"role": "assistant", "content": response.content})

        tool_rounds = 0
        while response.stop_reason == "tool_use" and tool_rounds < 5:
            tool_rounds += 1
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    result = registry.execute(block.name, **block.input)
                    current_messages.append({
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": block.id, "content": result}],
                    })

            response = client.messages.create(
                model=model,
                system=system_prompt,
                messages=current_messages,
                max_tokens=max_tokens,
                tools=registry.list_schemas(),
            )
            current_messages.append({"role": "assistant", "content": response.content})

        return response.content


# ── REPL ────────────────────────────────────────────────────────────────

def repl():
    print("=== OpenClaw s11: Gateway & API ===")
    print("The agent runs as an HTTP service with OpenAI-compatible API.\n")

    config = GatewayConfig()
    gateway = Gateway(config)
    gateway._start_time = time.time()
    gateway.start()

    host = config.host
    port = config.port
    model = config.model
    base_url = "http://" + host + ":" + str(port)
    print("Try these commands from another terminal:")
    print("  curl " + base_url + "/health")
    print('  curl -X POST ' + base_url + '/v1/chat/completions -H "Content-Type: application/json" -d ' + "'" + '{"model":"' + model + '","messages":[{"role":"user","content":"Say hello"}]}' + "'")
    print()
    print("Or type commands here:")
    print("  /test <message>  - send a test message via curl")
    print("  /health          - check health")
    print("  type 'q' to quit")
    print()

    messages = []

    while True:
        try:
            user_input = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("q", "quit", "exit"):
            break

        if user_input == "/health":
            import urllib.request
            try:
                resp = urllib.request.urlopen(f"http://{config.host}:{config.port}/health")
                print(json.dumps(json.loads(resp.read()), indent=2))
            except Exception as e:
                print(f"Error: {e}")
            continue

        if user_input.startswith("/test "):
            msg = user_input[6:]
            import urllib.request
            body = json.dumps({
                "model": config.model,
                "messages": [{"role": "user", "content": msg}],
            }).encode("utf-8")
            try:
                req = urllib.request.Request(
                    f"http://{config.host}:{config.port}/v1/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                resp = urllib.request.urlopen(req)
                result = json.loads(resp.read())
                choice = result["choices"][0]
                print(f"\n\033[33m[response]\033[0m {choice['message']['content']}\n")
            except Exception as e:
                print(f"Error: {e}")
            continue

        # Normal message: go through local agent loop
        messages.append({"role": "user", "content": user_input})
        content = gateway._run_agent(messages, config.model, SYSTEM_PROMPT, 4096)
        for block in content:
            if getattr(block, "type", None) == "text":
                print(block.text)

    gateway.stop()
    print("Shutdown complete.")


if __name__ == "__main__":
    repl()