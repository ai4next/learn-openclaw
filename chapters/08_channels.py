#!/usr/bin/env python3
"""Harness layer: Channel System

    +-----------+     +-----------+     +-----------+
    | CLI       | --> | Channel   | --> | Agent     |
    | File      | --> | Manager   |     |           |
    | Webhook   | --> |           |     |           |
    +-----------+     +-----------+     +-----------+
                            |
                     +-----------+
                     | Outbound  |
                     | Router    |
                     +-----------+
                            |
                    +------+------+
                    |      |      |
                   CLI   File  Webhook

Key insight: Channels abstract away transport. The agent doesn't
know or care if the user is on CLI, Telegram, or Discord.
It writes responses and the channel manager routes them.
"""

import json
import os
import sys
import time
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from queue import Queue

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Base Channel ────────────────────────────────────────────────────────

class BaseChannel(ABC):
    """Abstract base for all channels. A channel is a transport layer."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def send(self, message: str):
        """Send an outbound message through this channel."""
        ...

    def start(self):
        """Lifecycle: start the channel (open connections, etc.)."""
        pass

    def stop(self):
        """Lifecycle: stop the channel (close connections, etc.)."""
        pass


# ── CLI Channel ─────────────────────────────────────────────────────────

class CLIChannel(BaseChannel):
    """Reads from stdin, prints to stdout with channel prefix."""

    def __init__(self):
        super().__init__("cli")
        self._inbound = Queue()

    def send(self, message: str):
        print(f"\n\033[33m[CLI ->]\033[0m {message}\n")

    def add_input(self, text: str):
        self._inbound.put(text)

    def get_input(self) -> str | None:
        if not self._inbound.empty():
            return self._inbound.get_nowait()
        return None


# ── File Channel ────────────────────────────────────────────────────────

class FileChannel(BaseChannel):
    """Reads/writes messages from/to a JSON lines file."""

    def __init__(self, filepath: Path):
        super().__init__("file")
        self.filepath = filepath
        self._inbound = Queue()
        self._last_position = 0

    def start(self):
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if not self.filepath.exists():
            self.filepath.write_text("", encoding="utf-8")
        self._last_position = self.filepath.stat().st_size

    def send(self, message: str):
        entry = json.dumps({
            "channel": self.name,
            "direction": "outbound",
            "timestamp": datetime.now().isoformat(),
            "message": message,
        })
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def poll(self):
        """Check file for new inbound messages."""
        try:
            current_size = self.filepath.stat().st_size
            if current_size <= self._last_position:
                return
            with open(self.filepath, "r", encoding="utf-8") as f:
                f.seek(self._last_position)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("direction") == "inbound":
                            self._inbound.put(entry["message"])
                    except json.JSONDecodeError:
                        pass
                self._last_position = f.tell()
        except (OSError, ValueError):
            pass

    def get_input(self) -> str | None:
        self.poll()
        if not self._inbound.empty():
            return self._inbound.get_nowait()
        return None


# ── Log Channel ─────────────────────────────────────────────────────────

class LogChannel(BaseChannel):
    """Logs all messages with timestamps. Never receives inbound."""

    def __init__(self):
        super().__init__("log")
        self._entries = []

    def send(self, message: str):
        entry = f"[{datetime.now().isoformat()}] [{self.name}] {message}"
        self._entries.append(entry)
        print(f"\033[90m{entry}\033[0m")

    def get_log(self) -> list[str]:
        return list(self._entries)


# ── WebSocket Channel ─────────────────────────────────────────────────────

class WebSocketChannel(BaseChannel):
    """Simple WebSocket-like channel using asyncio.

    Unlike CLI/File channels which are synchronous, this demonstrates
    an async transport layer. The channel maintains a set of connected
    clients and broadcasts messages to all of them.
    """

    def __init__(self, host: str = "localhost", port: int = 8765):
        super().__init__("websocket")
        self.host = host
        self.port = port
        self._inbound = Queue()
        self._clients: set = set()
        self._server = None
        self._thread = None

    def start(self):
        """Start the WebSocket server in a background thread."""
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()

    def _run_server(self):
        """Minimal WS-like server using plain TCP sockets."""
        import socket
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(5)
        self._server.settimeout(1.0)

        while True:
            try:
                conn, addr = self._server.accept()
                self._clients.add(conn)
                threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn, addr):
        """Read messages from a single client connection."""
        try:
            conn.settimeout(1.0)
            buffer = b""
            while True:
                try:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            msg = line.decode("utf-8").strip()
                            if msg:
                                self._inbound.put(msg)
                        except UnicodeDecodeError:
                            pass
                except socket.timeout:
                    continue
        except (OSError, ConnectionResetError):
            pass
        finally:
            self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def send(self, message: str):
        """Broadcast to all connected clients."""
        payload = (message + "\n").encode("utf-8")
        dead = set()
        for conn in self._clients:
            try:
                conn.sendall(payload)
            except (OSError, BrokenPipeError):
                dead.add(conn)
        self._clients -= dead

    def get_input(self) -> str | None:
        if not self._inbound.empty():
            return self._inbound.get_nowait()
        return None

    def stop(self):
        if self._server:
            self._server.close()
        for conn in self._clients:
            try:
                conn.close()
            except OSError:
                pass
        self._clients.clear()

class ChannelManager:
    """Manages multiple channels. Routes inbound/outbound messages."""

    def __init__(self):
        self._channels: dict[str, BaseChannel] = {}

    def register(self, channel: BaseChannel):
        channel.start()
        self._channels[channel.name] = channel

    def get(self, name: str) -> BaseChannel | None:
        return self._channels.get(name)

    def send_all(self, message: str):
        """Send a message to all registered channels."""
        for channel in self._channels.values():
            channel.send(message)

    def send_to(self, channel_name: str, message: str):
        """Send a message to a specific channel."""
        channel = self.get(channel_name)
        if channel:
            channel.send(message)

    def get_messages(self) -> list[tuple[str, str]]:
        """Collect pending inbound messages as (channel_name, text)."""
        results = []
        for name, channel in self._channels.items():
            if hasattr(channel, "get_input"):
                msg = channel.get_input()
                if msg:
                    results.append((name, msg))
        return results

    def list_channels(self) -> list[str]:
        return list(self._channels.keys())

    def stop_all(self):
        for channel in self._channels.values():
            channel.stop()


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
        import subprocess
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


class SendChannelTool(Tool):
    """Tool that lets the agent send a message to a specific channel."""
    def __init__(self, channel_mgr: ChannelManager):
        self._mgr = channel_mgr
    @property
    def name(self): return "send_channel"
    @property
    def description(self): return "Send a message to a specific channel"
    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": f"Target channel: {', '.join(self._mgr.list_channels())}",
                },
                "message": {"type": "string", "description": "Message content"},
            },
            "required": ["channel", "message"],
        }
    def execute(self, channel="", message=""):
        self._mgr.send_to(channel, message)
        return f"Sent to channel '{channel}'"


# ── System Prompt ───────────────────────────────────────────────────────

registry = ToolRegistry()
for t in [BashTool(), ReadTool(), WriteTool()]:
    registry.register(t)

SYSTEM_PROMPT = """You are an OpenClaw agent with multi-channel messaging.
Messages show which channel they came from. You can respond to specific
channels using the send_channel tool.

The available channels are: cli, file, log"""


# ── Agent Loop ──────────────────────────────────────────────────────────

def agent_loop(messages):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM_PROMPT,
            messages=messages,
            max_tokens=4096,
            tools=registry.list_schemas(),
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            break
        if response.stop_reason == "tool_use":
            for block in response.content:
                if block.type == "tool_use":
                    result = registry.execute(block.name, **block.input)
                    messages.append({
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": block.id, "content": result}],
                    })
    return response.content


# ── REPL ────────────────────────────────────────────────────────────────

def repl():
    channel_mgr = ChannelManager()

    cli = CLIChannel()
    file_ch = FileChannel(WORKDIR / "channel_data.jsonl")
    log_ch = LogChannel()
    ws_ch = WebSocketChannel(host="localhost", port=8765)

    channel_mgr.register(cli)
    channel_mgr.register(file_ch)
    channel_mgr.register(log_ch)
    channel_mgr.register(ws_ch)

    registry.register(SendChannelTool(channel_mgr))

    messages = []
    print("=== OpenClaw s08: Channel System ===")
    print(f"Channels: {', '.join(channel_mgr.list_channels())}")
    print("Messages are tagged with their source channel.")
    print("Type 'q' to quit.")
    print("Prefix with 'file: ' to simulate a file channel message.")
    print("Type 'channels' to list active channels.")
    print(f"WebSocket server on ws://{ws_ch.host}:{ws_ch.port}")
    print("  Connect: nc {ws_ch.host} {ws_ch.port} (or telnet)\n")

    while True:
        try:
            user_input = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("q", "quit", "exit"):
            break

        if user_input.lower() == "channels":
            print(f"Active channels: {', '.join(channel_mgr.list_channels())}")
            continue

        # Simulate file channel input
        if user_input.startswith("file: "):
            actual_input = user_input[6:]
            entry = json.dumps({
                "channel": "file",
                "direction": "inbound",
                "timestamp": datetime.now().isoformat(),
                "message": actual_input,
            })
            with open(WORKDIR / "channel_data.jsonl", "a", encoding="utf-8") as f:
                f.write(entry + "\n")
            print(f"\033[33m[file ->]\033[0m {actual_input}")
            messages.append({"role": "user", "content": f"[file channel] {actual_input}"})
        else:
            messages.append({"role": "user", "content": f"[cli channel] {user_input}"})

        content = agent_loop(messages)
        for block in content:
            if block.type == "text":
                channel_mgr.send_all(block.text)

    channel_mgr.stop_all()
    print("\nShutdown complete.")


if __name__ == "__main__":
    repl()