#!/usr/bin/env python3
"""Harness layer: Subagent System

    +-----------+     +-----------+     +-----------+
    | Parent    | --> | Spawn     | --> | Subagent  |
    | Agent     |     | Tool      |     | Runner    |
    +-----------+     +-----------+     +-----------+
                            |                 |
                            v                 v
                     +-----------+     +-----------+
                     | Subagent  |     | Pending   |
                     | Manager   | --> | Queue     |
                     +-----------+     +-----------+
                                             |
                                             v
                                       +-----------+
                                       | Mid-turn  |
                                       | Injection |
                                       +-----------+

Key insight: A subagent is the same agent loop running in a
separate context. The parent spawns work, continues its own
turn, and the subagent's result arrives via the pending queue
for mid-turn injection.
"""

import json
import os
import queue as queue_module
import sys
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
PROMPT_COLOR = "\033[36ms13 >> \033[0m"


# ---------------------------------------------------------------------------
# Pending Queue (from s07) — mid-turn injection
# ---------------------------------------------------------------------------
class PendingQueue:
    """A per-session queue for mid-turn message injection."""

    def __init__(self, maxsize: int = 20):
        self._queue: queue_module.Queue = queue_module.Queue(maxsize=maxsize)

    def put(self, msg: str) -> None:
        self._queue.put_nowait(msg)

    def get_nowait(self) -> str | None:
        try:
            return self._queue.get_nowait()
        except queue_module.Empty:
            return None

    def drain(self) -> list[str]:
        items = []
        while True:
            msg = self.get_nowait()
            if msg is None:
                break
            items.append(msg)
        return items


# ---------------------------------------------------------------------------
# Subagent
# ---------------------------------------------------------------------------
class SubagentResult:
    """Result from a completed subagent run."""

    def __init__(self, task_id: str, content: str, status: str = "completed"):
        self.task_id = task_id
        self.content = content
        self.status = status


class Subagent:
    """A subagent runs the same agent loop in a separate context."""

    def __init__(self, task_id: str, prompt: str, client, model: str):
        self.task_id = task_id
        self.prompt = prompt
        self.client = client
        self.model = model
        self._messages: list = []

    def run(self, timeout: int = 120) -> SubagentResult:
        """Run the subagent with a simplified loop. Returns the final text."""
        self._messages.append({"role": "user", "content": self.prompt})
        try:
            response = self.client.messages.create(
                model=self.model,
                system=(
                    "You are a subagent. Complete the assigned task and report results concisely. "
                    "You have no tools — use reasoning only."
                ),
                messages=self._messages,
                max_tokens=4096,
            )
            content = "".join(b.text for b in response.content if b.type == "text")
            return SubagentResult(self.task_id, content.strip(), "completed")
        except Exception as e:
            return SubagentResult(self.task_id, f"Error: {e}", "error")


# ---------------------------------------------------------------------------
# SubagentManager
# ---------------------------------------------------------------------------
class SubagentManager:
    """Manages the lifecycle of spawned subagents."""

    def __init__(self, client, model: str):
        self.client = client
        self.model = model
        self._results: dict[str, SubagentResult] = {}
        self._pending: dict[str, Subagent] = {}
        self._lock = threading.Lock()

    def spawn(self, prompt: str) -> str:
        """Create and start a subagent in a background thread.

        Returns a task_id that can be used to collect results.
        """
        task_id = uuid.uuid4().hex[:12]
        agent = Subagent(task_id, prompt, self.client, self.model)

        with self._lock:
            self._pending[task_id] = agent

        thread = threading.Thread(target=self._run, args=(agent,), daemon=True)
        thread.start()
        return task_id

    def _run(self, agent: Subagent) -> None:
        """Run a subagent and store its result."""
        result = agent.run()
        with self._lock:
            self._results[agent.task_id] = result
            self._pending.pop(agent.task_id, None)

    def collect(self, task_id: str) -> SubagentResult | None:
        """Collect a subagent result if it's ready. Non-blocking."""
        with self._lock:
            return self._results.pop(task_id, None)

    def collect_all(self) -> list[SubagentResult]:
        """Collect all completed subagent results."""
        with self._lock:
            results = list(self._results.values())
            self._results.clear()
            return results

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def wait_all(self, timeout: float = 60.0) -> None:
        """Block until all pending subagents complete."""
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            with self._lock:
                if not self._pending:
                    return
            _time.sleep(0.1)


# ---------------------------------------------------------------------------
# SpawnTool
# ---------------------------------------------------------------------------
class Tool:
    def __init__(self, name, description, input_schema, execute):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.execute = execute

    def to_api_schema(self):
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _make_spawn_tool(mgr: SubagentManager):
    def execute(**kwargs):
        prompt = kwargs.get("prompt", "")
        if not prompt:
            return "No prompt provided."
        task_id = mgr.spawn(prompt)
        return f"Subagent spawned. Task ID: {task_id}. Pending: {mgr.pending_count()}"

    return Tool(
        name="spawn",
        description="Spawn a subagent to complete a task in the background.",
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task description for the subagent."}
            },
            "required": ["prompt"],
        },
        execute=execute,
    )


def _make_collect_tool(mgr: SubagentManager):
    def execute(**kwargs):
        task_id = kwargs.get("task_id", "")
        if not task_id:
            # Collect all available
            results = mgr.collect_all()
            if not results:
                return f"No completed subagent results. Pending: {mgr.pending_count()}"
            lines = [f"Collected {len(results)} subagent result(s):"]
            for r in results:
                lines.append(f"  [{r.task_id}] ({r.status}): {r.content[:200]}")
            return "\n".join(lines)
        result = mgr.collect(task_id)
        if result is None:
            return f"No result for task '{task_id}' (still running, or invalid ID). Pending: {mgr.pending_count()}"
        return f"[{result.task_id}] ({result.status}): {result.content}"

    return Tool(
        name="collect",
        description="Collect results from completed subagents.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Specific task ID, or empty to collect all."}
            },
        },
        execute=execute,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def repl():
    client = Anthropic()
    sub_mgr = SubagentManager(client, MODEL)
    pending_queue = PendingQueue()

    class ToolRegistry:
        def __init__(self):
            self._tools = {}
        def register(self, tool):
            self._tools[tool.name] = tool
        def list_schemas(self):
            return [t.to_api_schema() for t in self._tools.values()]
        def execute(self, name, **kwargs):
            t = self._tools.get(name)
            if not t:
                return f"Error: unknown tool '{name}'"
            return t.execute(**kwargs)

    registry = ToolRegistry()
    registry.register(_make_spawn_tool(sub_mgr))
    registry.register(_make_collect_tool(sub_mgr))

    print("=== OpenClaw s13: Subagent System ===")
    print("Spawn background subagents to parallelize work.")
    print("Commands:")
    print("  /spawn <prompt>       - spawn a subagent")
    print("  /collect [task_id]    - collect results")
    print("  /status               - show pending counts")
    print("  /wait                 - wait for all to complete")
    print("  q                     - quit\n")

    while True:
        try:
            user_input = input(PROMPT_COLOR)
        except (EOFError, KeyboardInterrupt):
            break

        raw = user_input.strip()

        if raw.lower() in ("q", "quit", "exit"):
            sub_mgr.wait_all(timeout=5)
            print("Goodbye.")
            break

        if raw.startswith("/spawn "):
            prompt = raw[7:].strip()
            task_id = sub_mgr.spawn(prompt)
            print(f"  Spawned subagent: {task_id}")
            continue

        if raw.startswith("/collect"):
            parts = raw.split(maxsplit=1)
            task_id = parts[1] if len(parts) > 1 else ""
            if task_id:
                result = sub_mgr.collect(task_id)
                if result:
                    print(f"  [{result.task_id}] ({result.status}):")
                    print(f"    {result.content[:500]}")
                else:
                    print(f"  No result for '{task_id}'. Pending: {sub_mgr.pending_count()}")
            else:
                results = sub_mgr.collect_all()
                if results:
                    for r in results:
                        print(f"  [{r.task_id}] ({r.status}): {r.content[:200]}")
                else:
                    print(f"  No completed results. Pending: {sub_mgr.pending_count()}")
            continue

        if raw == "/status":
            print(f"  Pending: {sub_mgr.pending_count()}")
            with sub_mgr._lock:
                ready = len(sub_mgr._results)
            print(f"  Ready: {ready}")
            continue

        if raw == "/wait":
            print("  Waiting for all subagents to complete...")
            sub_mgr.wait_all(timeout=30)
            print(f"  Done. Pending: {sub_mgr.pending_count()}")
            continue

        print(f"  Unknown command. Try /spawn, /collect, /status, /wait")


if __name__ == "__main__":
    repl()