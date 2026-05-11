#!/usr/bin/env python3
"""Harness layer: Message Bus

    +-----------+     +-----------+     +-----------+
    | Producers | --> |  Message  | --> | Consumers |
    | (channels)|     |   Bus     |     | (agent)   |
    +-----------+     +-----------+     +-----------+
                      |  Inbound  |
                      |  Queue    |
                      +-----------+
                      |  Outbound |
                      |  Queue    |
                      +-----------+
                      |  Pending  |  <- mid-turn injection
                      |  Queue    |     (subagent results)
                      +-----------+

Key insight: A message bus decouples who sends from who processes.
Channels publish; the agent loop consumes.

The PendingQueue extends this: during agent processing, sub-agents
or follow-up messages can be injected mid-turn without starting a
new dispatch cycle.
"""

import json
import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")

PROMPT_COLOR = "\033[36ms06 >> \033[0m"


# ---------------------------------------------------------------------------
# Tool base
# ---------------------------------------------------------------------------
class Tool:
    def __init__(self, name, description, input_schema, execute):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.execute = execute

    def to_api_dict(self):
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------
def _make_bash_tool():
    import subprocess

    def execute(**kwargs):
        command = kwargs.get("command", "")
        if not command:
            return "No command provided."
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout or ""
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out (60s)."
        except Exception as e:
            return f"Error: {e}"

    return Tool(
        name="bash",
        description="Execute a bash command (non-interactive, 60s timeout).",
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to run.",
                }
            },
            "required": ["command"],
        },
        execute=execute,
    )


def _make_read_tool():
    def execute(**kwargs):
        path = kwargs.get("path", "")
        if not path:
            return "No path provided."
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        try:
            return p.read_text(encoding="utf-8")
        except Exception as e:
            return f"Error reading file: {e}"

    return Tool(
        name="read",
        description="Read the contents of a file on disk.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                }
            },
            "required": ["path"],
        },
        execute=execute,
    )


def _make_write_tool():
    def execute(**kwargs):
        path = kwargs.get("path", "")
        content = kwargs.get("content", "")
        if not path:
            return "No path provided."
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {e}"

    return Tool(
        name="write",
        description="Write content to a file on disk (creates parent dirs).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "content": {
                    "type": "string",
                    "description": "File content.",
                },
            },
            "required": ["path", "content"],
        },
        execute=execute,
    )


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------
@dataclass
class InboundMessage:
    """A message coming into the bus from a channel."""

    channel: str
    sender: str
    content: str
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __str__(self):
        return f"[{self.channel}:{self.sender}] {self.content[:80]}"


@dataclass
class OutboundMessage:
    """A message going out of the bus to a channel."""

    channel: str
    content: str
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __str__(self):
        return f"[{self.channel}] {self.content[:80]}"


# ---------------------------------------------------------------------------
# MessageBus
# ---------------------------------------------------------------------------
class MessageBus:
    """A simple in-memory message bus using deques for inbound/outbound queues.

    Supports multiple channels. Thread-safe via a lock.
    """

    def __init__(self):
        self._inbound: deque[InboundMessage] = deque()
        self._outbound: deque[OutboundMessage] = deque()
        self._lock = threading.Lock()

    # -- Inbound ----------------------------------------------------------

    def publish_inbound(self, msg: InboundMessage) -> None:
        with self._lock:
            self._inbound.append(msg)

    def consume_inbound(self) -> Optional[InboundMessage]:
        with self._lock:
            if self._inbound:
                return self._inbound.popleft()
            return None

    def drain_inbound(self) -> list[InboundMessage]:
        """Consume all currently queued inbound messages."""
        result = []
        with self._lock:
            while self._inbound:
                result.append(self._inbound.popleft())
        return result

    # -- Outbound ---------------------------------------------------------

    def publish_outbound(self, msg: OutboundMessage) -> None:
        with self._lock:
            self._outbound.append(msg)

    def consume_outbound(self) -> Optional[OutboundMessage]:
        with self._lock:
            if self._outbound:
                return self._outbound.popleft()
            return None

    def drain_outbound(self) -> list[OutboundMessage]:
        """Consume all currently queued outbound messages."""
        result = []
        with self._lock:
            while self._outbound:
                result.append(self._outbound.popleft())
        return result

    # -- Status -----------------------------------------------------------

    def inbound_count(self) -> int:
        with self._lock:
            return len(self._inbound)

    def outbound_count(self) -> int:
        with self._lock:
            return len(self._outbound)


# ---------------------------------------------------------------------------
# PendingQueue — mid-turn message injection
# ---------------------------------------------------------------------------
class PendingQueue:
    """A per-session queue for mid-turn message injection.

    While an agent is processing a turn, follow-up messages (e.g. subagent
    results) can be queued here. The agent loop drains this queue between
    tool iterations and injects the messages as user turns.

    This is the foundation for the Subagent system (s14).
    """

    def __init__(self, maxsize: int = 20):
        self._queue: queue.Queue[InboundMessage] = queue.Queue(maxsize=maxsize)

    def put(self, msg: InboundMessage) -> None:
        """Queue a follow-up message for mid-turn injection."""
        self._queue.put_nowait(msg)

    def get_nowait(self) -> Optional[InboundMessage]:
        """Dequeue a message if one is available (non-blocking)."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> list[InboundMessage]:
        """Dequeue all currently available messages."""
        items = []
        while True:
            msg = self.get_nowait()
            if msg is None:
                break
            items.append(msg)
        return items

    def __len__(self) -> int:
        return self._queue.qsize()


# ---------------------------------------------------------------------------
# Simulated channel producers (background threads)
# ---------------------------------------------------------------------------
def _simulate_web_channel(bus: MessageBus, stop_event: threading.Event):
    """Simulate a web API sending messages periodically."""
    import random

    endpoints = ["/api/status", "/api/data", "/api/health"]
    while not stop_event.is_set():
        time.sleep(random.uniform(8.0, 15.0))
        if stop_event.is_set():
            break
        msg = InboundMessage(
            channel="web",
            sender=f"endpoint:{random.choice(endpoints)}",
            content=f"Web request received at {time.strftime('%H:%M:%S')}",
            metadata={"method": "GET", "ip": f"10.0.0.{random.randint(1, 255)}"},
        )
        bus.publish_inbound(msg)


def _simulate_api_channel(bus: MessageBus, stop_event: threading.Event):
    """Simulate an external API callback."""
    import random

    while not stop_event.is_set():
        time.sleep(random.uniform(10.0, 20.0))
        if stop_event.is_set():
            break
        msg = InboundMessage(
            channel="api",
            sender="webhook",
            content=f"API callback: task_{random.randint(100, 999)} completed",
            metadata={"status": "success", "duration_ms": random.randint(50, 5000)},
        )
        bus.publish_inbound(msg)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------
def repl():
    client = Anthropic()
    bus = MessageBus()

    tools = [
        _make_bash_tool(),
        _make_read_tool(),
        _make_write_tool(),
    ]
    api_tools = [t.to_dict() for t in tools]

    system_prompt = (
        "You are an AI assistant connected to a message bus. "
        "Messages arrive from multiple channels: cli (your direct input), "
        "web (simulated web requests), and api (simulated API callbacks). "
        "Process them as they come in."
    )

    messages = []

    # Start simulated channel producers in background threads
    stop_event = threading.Event()
    web_thread = threading.Thread(
        target=_simulate_web_channel, args=(bus, stop_event), daemon=True
    )
    api_thread = threading.Thread(
        target=_simulate_api_channel, args=(bus, stop_event), daemon=True
    )
    web_thread.start()
    api_thread.start()

    # The REPL itself is the "cli" channel producer
    current_channel = "cli"
    current_sender = "user"

    print(f"{PROMPT_COLOR}Message Bus agent started.")
    print(f"{PROMPT_COLOR}Channels: cli (you), web (simulated), api (simulated)")
    print(f"{PROMPT_COLOR}Commands: /inbox  (drain queued inbound messages)")
    print(f"{PROMPT_COLOR}          /outbox (drain queued outbound messages)")
    print(f"{PROMPT_COLOR}          /channel <name> (switch input channel)")
    print(f"{PROMPT_COLOR}          /bus    (show queue sizes)")
    print(f"{PROMPT_COLOR}          q       (quit)")
    print()

    while True:
        try:
            user_input = input(f"{PROMPT_COLOR}")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        raw = user_input.strip()

        if raw.lower() == "q":
            print(f"{PROMPT_COLOR}Shutting down bus...")
            stop_event.set()
            web_thread.join(timeout=2)
            api_thread.join(timeout=2)
            print(f"{PROMPT_COLOR}Goodbye.")
            break

        # -- REPL commands --
        if raw == "/inbox":
            msgs = bus.drain_inbound()
            if msgs:
                print(f"{PROMPT_COLOR}--- Inbound Messages ({len(msgs)}) ---")
                for m in msgs:
                    print(
                        f"{PROMPT_COLOR}  [{m.channel}:{m.sender}] {m.content}"
                        f"  meta={m.metadata}"
                    )
            else:
                print(f"{PROMPT_COLOR}No inbound messages.")
            continue

        if raw == "/outbox":
            msgs = bus.drain_outbound()
            if msgs:
                print(f"{PROMPT_COLOR}--- Outbound Messages ({len(msgs)}) ---")
                for m in msgs:
                    print(f"{PROMPT_COLOR}  [{m.channel}] {m.content}")
            else:
                print(f"{PROMPT_COLOR}No outbound messages.")
            continue

        if raw.startswith("/channel "):
            name = raw[len("/channel "):].strip()
            if name in ("cli", "web", "api"):
                current_channel = name
                print(f"{PROMPT_COLOR}Switched to channel: {name}")
            else:
                print(f"{PROMPT_COLOR}Unknown channel. Use: cli, web, api")
            continue

        if raw == "/bus":
            print(
                f"{PROMPT_COLOR}Bus: {bus.inbound_count()} inbound,"
                f" {bus.outbound_count()} outbound"
            )
            continue

        # -- Post user input as an InboundMessage on the current channel --
        msg = InboundMessage(
            channel=current_channel,
            sender=current_sender,
            content=raw,
        )
        bus.publish_inbound(msg)

        # Collect all pending inbound messages (including background ones)
        pending = bus.drain_inbound()
        if not pending:
            continue

        # Build a single user-turn from all pending messages
        combined = "\n".join(
            f"[{m.channel}:{m.sender}] {m.content}" for m in pending
        )
        messages.append({"role": "user", "content": combined})

        # -- Agent loop --
        while True:
            try:
                response = client.messages.create(
                    model=MODEL,
                    system=system_prompt,
                    max_tokens=4096,
                    messages=messages,
                    tools=api_tools,
                )
            except Exception as e:
                print(f"{PROMPT_COLOR}API error: {e}")
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.model_dump()["content"]})

                for block in response.content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input if hasattr(block, "input") else {}
                        matched = [t for t in tools if t.name == tool_name]
                        if matched:
                            result = matched[0].execute(**tool_input)
                            print(
                                f"{PROMPT_COLOR}[Tool: {tool_name}]\n"
                                f"{PROMPT_COLOR}  Result: {result[:200]}{'...' if len(result) > 200 else ''}"
                            )
                            # Publish tool result as outbound message
                            bus.publish_outbound(
                                OutboundMessage(
                                    channel="agent",
                                    content=f"Tool {tool_name} returned: {result[:100]}",
                                )
                            )
                            messages.append(
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "tool_result",
                                            "tool_use_id": block.id,
                                            "content": str(result),
                                        }
                                    ],
                                }
                            )
                        else:
                            messages.append(
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "tool_result",
                                            "tool_use_id": block.id,
                                            "content": f"Unknown tool: {tool_name}",
                                        }
                                    ],
                                }
                            )
                continue

            elif response.stop_reason == "end_turn":
                for block in response.content:
                    if block.type == "text":
                        print(f"{PROMPT_COLOR}{block.text}")
                        bus.publish_outbound(
                            OutboundMessage(channel="agent", content=block.text)
                        )
                messages.append({"role": "assistant", "content": response.model_dump()["content"]})
                break

            else:
                print(f"{PROMPT_COLOR}Unexpected stop_reason: {response.stop_reason}")
                break


if __name__ == "__main__":
    repl()