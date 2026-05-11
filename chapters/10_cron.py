#!/usr/bin/env python3
"""Harness layer: Cron & Scheduling

    +-----------+
    | Cron      |  Schedule: "every 30s",
    | Service   |  "at 09:00", "*/5 * * * *"
    +-----------+
         |
    +-----------+
    | Agent     |  Autonomous ticks
    | Ticker    |  with heartbeat prompt
    +-----------+

Key insight: An agent that only responds is half an agent.
Scheduling enables autonomous operation — the agent checks
in, observes, and acts without being asked.
"""

import json
import os
import sys
import time
import threading
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Cron Types ──────────────────────────────────────────────────────────

@dataclass
class CronJob:
    name: str
    interval_seconds: int
    action_prompt: str
    last_run: float = 0.0
    enabled: bool = True


# ── Cron Parser ─────────────────────────────────────────────────────────

class CronParser:
    """Parse human-readable schedule expressions."""

    @staticmethod
    def parse(expression: str) -> int:
        """Parse expression like 'every 30s', 'every 5m', 'every 1h'.
        Returns interval in seconds. Raises ValueError on invalid input."""
        expr = expression.strip().lower()

        if expr.startswith("every "):
            rest = expr[6:].strip()
            # Try "every 30s", "every 5m", "every 1h"
            if rest.endswith("s"):
                return int(rest[:-1])
            elif rest.endswith("m"):
                return int(rest[:-1]) * 60
            elif rest.endswith("h"):
                return int(rest[:-1]) * 3600
            elif rest.endswith("sec"):
                return int(rest[:-3])
            elif rest.endswith("min"):
                return int(rest[:-3]) * 60
            else:
                # Try plain number (seconds)
                try:
                    return int(rest)
                except ValueError:
                    pass

        raise ValueError(f"Unrecognized schedule: '{expression}'. Use 'every Xs', 'every Xm', or 'every Xh'.")


# ── Cron Service ────────────────────────────────────────────────────────

class CronService:
    """Runs cron jobs in a background thread. Fires due jobs and
    injects tick messages into the agent conversation."""

    def __init__(self):
        self._jobs: dict[str, CronJob] = {}
        self._tick_queue: Queue = Queue()
        self._running = False
        self._thread: threading.Thread | None = None

    def add_job(self, job: CronJob):
        self._jobs[job.name] = job
        print(f"  Cron job added: '{job.name}' every {job.interval_seconds}s")

    def remove_job(self, name: str) -> bool:
        if name in self._jobs:
            del self._jobs[name]
            return True
        return False

    def get_job(self, name: str) -> CronJob | None:
        return self._jobs.get(name)

    def list_jobs(self) -> list[CronJob]:
        return list(self._jobs.values())

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print("  Cron service started (background thread)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        print("  Cron service stopped")

    def get_tick(self) -> str | None:
        """Get the next pending tick message, if any."""
        if not self._tick_queue.empty():
            return self._tick_queue.get_nowait()
        return None

    def _run_loop(self):
        while self._running:
            now = time.time()
            for job in self._jobs.values():
                if not job.enabled:
                    continue
                if now - job.last_run >= job.interval_seconds:
                    job.last_run = now
                    tick_msg = (
                        f"[CRON TICK] Job '{job.name}' fired. "
                        f"Action: {job.action_prompt}"
                    )
                    self._tick_queue.put(tick_msg)
            time.sleep(1)


# ── Heartbeat Service ────────────────────────────────────────────────────

class HeartbeatService:
    """Periodic agent wake-up to check for tasks via HEARTBEAT.md.

    Unlike CronService which injects inline tick messages, HeartbeatService
    uses a two-phase pattern:
      Phase 1 (decision): reads HEARTBEAT.md, asks LLM via a virtual
        tool call whether there are active tasks (skip or run).
      Phase 2 (execution): only when Phase 1 returns 'run', executes
        the task through the agent loop.

    This pattern avoids the LLM producing free-text status messages
    when there is nothing to report.
    """

    def __init__(self, heartbeat_file: Path, client, model: str, interval_s: int = 300):
        self.heartbeat_file = heartbeat_file
        self.client = client
        self.model = model
        self.interval_s = interval_s
        self._running = False
        self._tick_queue: Queue = Queue()
        self._thread: threading.Thread | None = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def get_tick(self) -> str | None:
        if not self._tick_queue.empty():
            return self._tick_queue.get_nowait()
        return None

    def _run_loop(self):
        while self._running:
            time.sleep(self.interval_s)
            if not self._running:
                break
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        if not self.heartbeat_file.exists():
            return
        content = self.heartbeat_file.read_text(encoding="utf-8").strip()
        if not content:
            return
        action = self._decide(content)
        if action == "run":
            self._tick_queue.put(
                f"[Heartbeat] HEARTBEAT.md indicates active tasks: {content[:200]}"
            )

    def _decide(self, content: str) -> str:
        """Phase 1: LLM decides if there are active tasks via virtual tool call."""
        HEARTBEAT_TOOL = [
            {
                "type": "function",
                "function": {
                    "name": "heartbeat",
                    "description": "Report heartbeat decision.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["skip", "run"],
                                "description": "skip = nothing to do, run = has active tasks",
                            },
                        },
                        "required": ["action"],
                    },
                },
            }
        ]
        try:
            response = self.client.messages.create(
                model=self.model,
                system="You are a heartbeat agent. Call the heartbeat tool to report your decision.",
                messages=[{"role": "user", "content": content[:4000]}],
                tools=HEARTBEAT_TOOL,
                max_tokens=128,
            )
            for block in response.content:
                if block.type == "tool_use" and block.name == "heartbeat":
                    return block.input.get("action", "skip")
        except Exception:
            pass
        return "skip"


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


# ── Setup ───────────────────────────────────────────────────────────────

registry = ToolRegistry()
for t in [BashTool(), ReadTool(), WriteTool()]:
    registry.register(t)

cron_service = CronService()

# Add a default job
cron_service.add_job(CronJob(
    name="heartbeat",
    interval_seconds=30,
    action_prompt="check current status and report",
))
cron_service.start()

SYSTEM_PROMPT = """You are an OpenClaw agent with cron/scheduling.

When you receive a [CRON TICK] message, it means a scheduled job has fired.
Respond to the tick with the requested action (e.g., status check, report).

Available cron jobs:
- heartbeat: every 30s, checks status

You have standard tools (bash, read, write)."""


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
    messages = []
    print("=== OpenClaw s10: Cron & Scheduling ===")
    print("Cron jobs run in the background and inject tick messages.")
    print("Type 'q' to quit.")
    print("Commands:")
    print("  /cron list          - list cron jobs")
    print("  /cron add <expr> <prompt>  - add a job (e.g. /cron add every 15s \"check load\")")
    print("  /cron remove <name> - remove a job")
    print("  /cron pause <name>  - disable a job")
    print("  /cron resume <name> - enable a job\n")

    try:
        while True:
            # Check for cron ticks
            tick = cron_service.get_tick()
            if tick:
                print(f"\n\033[91m[CRON]\033[0m {tick}")
                messages.append({"role": "user", "content": tick})
                content = agent_loop(messages)
                for block in content:
                    if block.type == "text":
                        print(block.text)

            # Prompt
            try:
                user_input = input("\033[36ms10 >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break

            if user_input.lower() in ("q", "quit", "exit"):
                break

            # Cron commands
            if user_input.startswith("/cron "):
                parts = user_input[6:].strip().split(maxsplit=2)
                cmd = parts[0] if parts else ""

                if cmd == "list":
                    jobs = cron_service.list_jobs()
                    if not jobs:
                        print("  No cron jobs.")
                    else:
                        for j in jobs:
                            status = "enabled" if j.enabled else "paused"
                            print(f"  {j.name}: every {j.interval_seconds}s [{status}] -> {j.action_prompt}")

                elif cmd == "add" and len(parts) >= 3:
                    expr = parts[1]
                    prompt_text = parts[2]
                    try:
                        interval = CronParser.parse(expr)
                        name = f"job_{len(cron_service.list_jobs()) + 1}"
                        cron_service.add_job(CronJob(name=name, interval_seconds=interval, action_prompt=prompt_text))
                        print(f"  Added cron job '{name}' (every {interval}s)")
                    except ValueError as e:
                        print(f"  Error: {e}")

                elif cmd == "remove" and len(parts) >= 2:
                    if cron_service.remove_job(parts[1]):
                        print(f"  Removed job '{parts[1]}'")
                    else:
                        print(f"  Job '{parts[1]}' not found")

                elif cmd == "pause" and len(parts) >= 2:
                    job = cron_service.get_job(parts[1])
                    if job:
                        job.enabled = False
                        print(f"  Paused job '{parts[1]}'")
                    else:
                        print(f"  Job '{parts[1]}' not found")

                elif cmd == "resume" and len(parts) >= 2:
                    job = cron_service.get_job(parts[1])
                    if job:
                        job.enabled = True
                        print(f"  Resumed job '{parts[1]}'")
                    else:
                        print(f"  Job '{parts[1]}' not found")

                else:
                    print("  Usage: /cron list|add|remove|pause|resume [...]")
                continue

            # Normal message
            messages.append({"role": "user", "content": user_input})
            content = agent_loop(messages)
            for block in content:
                if block.type == "text":
                    print(block.text)

    finally:
        cron_service.stop()
        print("Shutdown complete.")


if __name__ == "__main__":
    repl()