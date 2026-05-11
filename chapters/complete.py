#!/usr/bin/env python3
"""Capstone: All 14 Sessions Combined into One Integrated Agent

    +----------+  +----------+  +----------+  +----------+  +-----------+
    | Tools    |  | Skills   |  | Memory   |  | Context  |  | Security  |
    | (s02)    |  | (s03)    |  | (s04)    |  | (s05)    |  | (s12)    |
    +-----+----+  +-----+----+  +----+-----+  +----+-----+  +-----+----+
          |             |             |             |             |
    +----------------------------------------------------------------+
    |                    Agent Loop  (s01)                            |
    |  tool_use dispatch | stop_reason | multi-turn conversation     |
    +----------------------------------------------------------------+
          |        |          |          |          |          |
    +-----+--+ +---+------+ +--+----+ +--+-----+ +--+-----+ +--+-----+
    | Msg    | | Session  | | Chan  | | Config | | Cron   | | Subag  |
    | Bus    | | Mgr      | | Mgr   | | Loader | | Srv    | | Mgr    |
    | (s06)  | | (s07)    | | (s08) | | (s09)  | | (s10)  | | (s13)  |
    +--------+ +----------+ +-------+ +--------+ +--------+ +--------+
          |
    +-----+------+
    | Gateway    |  HTTP /v1/chat/completions (s11)
    +------------+
    | Dream      |  Background memory consolidation (s14)
    +------------+

Key insight: The harness is a composition of loosely coupled subsystems.
Each chapter adds one mechanism; the capstone wires them all together.
"""

import http.server
import json
import os
import queue
import socketserver
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ═══════════════════════════════════════════════════════════════════════
# 1. TOOL SYSTEM (s02) — with lifecycle hooks + security (s12)
# ═══════════════════════════════════════════════════════════════════════

class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...
    @property
    @abstractmethod
    def description(self) -> str: ...
    @property
    @abstractmethod
    def parameters(self) -> dict: ...
    @property
    def concurrency_safe(self) -> bool: return False

    def to_api_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}

    def before_execute(self, **kwargs) -> dict: return kwargs
    @abstractmethod
    def execute(self, **kwargs) -> str: ...
    def after_execute(self, result: str, **kwargs) -> str: return result


class ToolError(Exception): pass
class ToolPermissionError(ToolError): pass
class ToolSystemError(ToolError): pass


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
    def register(self, tool: Tool):
        self._tools[tool.name] = tool
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)
    def list_schemas(self) -> list[dict]:
        return [t.to_api_schema() for t in self._tools.values()]
    def list_names(self) -> list[str]:
        return list(self._tools.keys())
    def execute(self, name: str, **kwargs) -> str:
        tool = self.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            validated = tool.before_execute(**kwargs)
            result = tool.execute(**validated)
            return tool.after_execute(result=result, **validated)
        except ToolPermissionError as e:
            return f"Permission denied: {e}\n(This is a hard policy boundary, do not retry.)"
        except ToolError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"System error: {e}"


def resolve_path(path: str, workspace: Path) -> Path:
    """Workspace-restricted path resolution (s12 Security)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = workspace / p
    resolved = p.resolve()
    try:
        resolved.relative_to(workspace.resolve())
        return resolved
    except ValueError:
        raise ToolPermissionError(f"path '{path}' is outside workspace")


class BashTool(Tool):
    name = "bash"
    description = "Execute a shell command"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    concurrency_safe = True

    def before_execute(self, command: str) -> dict:
        dangerous = ["rm -rf /", ":(){ :|:& };:", "dd if=/dev/zero of="]
        if any(d in command for d in dangerous):
            raise ToolPermissionError(f"dangerous command pattern")
        return {"command": command}

    def execute(self, command=""):
        try:
            r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            out = r.stdout or ""
            if r.stderr: out += f"\nSTDERR:\n{r.stderr}"
            if r.returncode != 0: out += f"\nExit code: {r.returncode}"
            return out or "(no output)"
        except subprocess.TimeoutExpired: raise ToolSystemError("command timed out")
        except Exception as e: raise ToolSystemError(str(e))


class ReadTool(Tool):
    name = "read"
    description = "Read a file from the workspace"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    concurrency_safe = True

    def execute(self, file_path=""):
        p = resolve_path(file_path, WORKDIR)
        if not p.exists(): raise ToolError(f"file not found: {file_path}")
        return p.read_text(encoding="utf-8")


class WriteTool(Tool):
    name = "write"
    description = "Write content to a file"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}

    def execute(self, file_path="", content=""):
        p = resolve_path(file_path, WORKDIR)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes"


# ═══════════════════════════════════════════════════════════════════════
# 2. SKILLS (s03)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Skill:
    name: str
    description: str
    body: str

class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self._skills: dict[str, Skill] = {}

    def discover(self):
        self._skills = {}
        if not self.skills_dir.exists(): return self._skills
        for skill_dir in self.skills_dir.iterdir():
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                content = skill_file.read_text(encoding="utf-8")
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        fm, body = parts[1], parts[2].strip()
                        name = self._extract(fm, "name") or skill_dir.name
                        desc = self._extract(fm, "description") or ""
                        self._skills[name] = Skill(name=name, description=desc, body=body)
        return self._skills

    @staticmethod
    def _extract(fm: str, key: str, default: str = "") -> str:
        for line in fm.splitlines():
            if line.strip().startswith(f"{key}:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
        return default

    def get_summary(self) -> str:
        if not self._skills: return "No skills available."
        return "Available skills:\n" + "\n".join(f"  - {s.name}: {s.description}" for s in self._skills.values())
    def list_names(self) -> list[str]: return list(self._skills.keys())


# ═══════════════════════════════════════════════════════════════════════
# 3. MEMORY (s04)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class MemoryEntry:
    key: str
    value: str
    timestamp: float = field(default_factory=time.time)

class MemoryStore:
    def __init__(self):
        self._entries: dict[str, MemoryEntry] = {}
    def put(self, key: str, value: str):
        self._entries[key] = MemoryEntry(key=key, value=value)
    def get(self, key: str) -> str | None:
        e = self._entries.get(key); return e.value if e else None
    def search(self, q: str) -> list[MemoryEntry]:
        ql = q.lower(); return [e for e in self._entries.values() if ql in e.key.lower() or ql in e.value.lower()]
    def delete(self, key: str): self._entries.pop(key, None)
    def list_keys(self) -> list[str]: return list(self._entries.keys())
    def to_string(self) -> str:
        if not self._entries: return "(empty)"
        return "\n".join(f"  {k}: {v.value[:80]}{'...' if len(v.value) > 80 else ''}" for k, v in self._entries.items())


# ═══════════════════════════════════════════════════════════════════════
# 4. CONTEXT MANAGEMENT (s05)
# ═══════════════════════════════════════════════════════════════════════

class TokenCounter:
    @staticmethod
    def count(text: str) -> int: return max(1, len(text) // 4)
    @staticmethod
    def count_messages(messages: list) -> int:
        total = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str): total += TokenCounter.count(content)
            elif isinstance(content, list):
                for b in content:
                    t = b.get("text") or b.get("content") or "" if isinstance(b, dict) else str(b)
                    total += TokenCounter.count(str(t))
        return total

class ContextManager:
    def __init__(self, max_tokens: int = 8000):
        self.max_tokens = max_tokens
        self._compacted = False
    def should_compact(self, messages: list) -> bool:
        return TokenCounter.count_messages(messages) > self.max_tokens
    def compact(self, messages: list) -> list:
        if len(messages) <= 4: return messages
        kept = messages[:2]
        remaining = messages[2:]
        if len(remaining) > 20:
            kept.append({"role": "user", "content": "[Earlier conversation compacted]"})
            kept.extend(remaining[-20:])
        else: kept.extend(remaining)
        self._compacted = True
        return kept
    def was_compacted(self) -> bool: return self._compacted


# ═══════════════════════════════════════════════════════════════════════
# 5. MESSAGE BUS (s06)
# ═══════════════════════════════════════════════════════════════════════

class MessageBus:
    def __init__(self):
        self._inbound: queue.Queue = queue.Queue()
        self._outbound: queue.Queue = queue.Queue()
    def publish_inbound(self, msg: dict): self._inbound.put(msg)
    def publish_outbound(self, msg: dict): self._outbound.put(msg)
    def poll_inbound(self, timeout=0.1) -> dict | None:
        try: return self._inbound.get(timeout=timeout)
        except queue.Empty: return None
    def poll_outbound(self) -> dict | None:
        try: return self._outbound.get_nowait()
        except queue.Empty: return None


# ═══════════════════════════════════════════════════════════════════════
# 6. SESSION (s07)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Session:
    id: str
    created: float = field(default_factory=time.time)
    messages: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

class SessionManager:
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._active_id: str | None = None
    def create(self, sid: str | None = None) -> Session:
        sid = sid or f"sess_{int(time.time())}"
        s = Session(id=sid); self._sessions[sid] = s; self._active_id = sid; return s
    def get_active(self) -> Session | None:
        return self._sessions.get(self._active_id) if self._active_id else None
    def switch_to(self, sid: str) -> bool:
        if sid in self._sessions: self._active_id = sid; return True
        return False
    def list_sessions(self) -> list[Session]: return list(self._sessions.values())
    def save(self):
        fp = self.storage_dir / "sessions.jsonl"
        with open(fp, "w", encoding="utf-8") as f:
            for s in self._sessions.values():
                f.write(json.dumps({"id": s.id, "created": s.created, "messages": s.messages, "metadata": s.metadata}) + "\n")
    def load(self):
        fp = self.storage_dir / "sessions.jsonl"
        if not fp.exists(): return
        for line in fp.read_text().strip().splitlines():
            if not line: continue
            d = json.loads(line)
            self._sessions[d["id"]] = Session(id=d["id"], created=d.get("created", 0), messages=d.get("messages", []), metadata=d.get("metadata", {}))
        if self._sessions and not self._active_id:
            self._active_id = list(self._sessions.keys())[-1]


# ═══════════════════════════════════════════════════════════════════════
# 7. CHANNELS (s08)
# ═══════════════════════════════════════════════════════════════════════

class BaseChannel(ABC):
    def __init__(self, name: str): self.name = name
    @abstractmethod
    def send(self, message: str): ...
    def start(self): ...
    def stop(self): ...

class CLIChannel(BaseChannel):
    def __init__(self): super().__init__("cli")
    def send(self, message: str): print(f"{message}")

class LogChannel(BaseChannel):
    def __init__(self):
        super().__init__("log"); self._entries = []
    def send(self, message: str):
        e = f"[{datetime.now().isoformat()}] {message}"
        self._entries.append(e)
    def get_log(self): return list(self._entries)

class ChannelManager:
    def __init__(self): self._channels: dict[str, BaseChannel] = {}
    def register(self, ch: BaseChannel): ch.start(); self._channels[ch.name] = ch
    def send_all(self, msg: str):
        for ch in self._channels.values(): ch.send(msg)
    def list_channels(self): return list(self._channels.keys())


# ═══════════════════════════════════════════════════════════════════════
# 8. CONFIG (s09)
# ═══════════════════════════════════════════════════════════════════════

class ConfigLoader:
    @staticmethod
    def load(path: Path) -> dict:
        if not path.exists(): return {}
        return json.loads(path.read_text(encoding="utf-8"))
    @staticmethod
    def save(config: dict, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
# 9. CRON + HEARTBEAT (s10)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class CronJob:
    name: str; interval_seconds: int; action_prompt: str
    last_run: float = 0.0; enabled: bool = True

class CronParser:
    @staticmethod
    def parse(expression: str) -> int:
        e = expression.strip().lower()
        if e.startswith("every "):
            r = e[6:].strip()
            if r.endswith("s"): return int(r[:-1])
            if r.endswith("m"): return int(r[:-1]) * 60
            if r.endswith("h"): return int(r[:-1]) * 3600
            try: return int(r)
            except ValueError: pass
        raise ValueError(f"Unrecognized schedule: '{expression}'")

class CronService:
    def __init__(self):
        self._jobs: dict[str, CronJob] = {}
        self._tick_queue: queue.Queue = queue.Queue()
        self._running = False; self._thread: threading.Thread | None = None
    def add_job(self, job: CronJob): self._jobs[job.name] = job
    def remove_job(self, name: str) -> bool: return self._jobs.pop(name, None) is not None
    def get_job(self, name: str) -> CronJob | None: return self._jobs.get(name)
    def list_jobs(self) -> list[CronJob]: return list(self._jobs.values())
    def start(self):
        if self._running: return
        self._running = True; self._thread = threading.Thread(target=self._run_loop, daemon=True); self._thread.start()
    def stop(self):
        self._running = False
        if self._thread: self._thread.join(timeout=2)
    def get_tick(self) -> str | None:
        try: return self._tick_queue.get_nowait()
        except queue.Empty: return None
    def _run_loop(self):
        while self._running:
            now = time.time()
            for j in self._jobs.values():
                if j.enabled and now - j.last_run >= j.interval_seconds:
                    j.last_run = now
                    self._tick_queue.put(f"[CRON] Job '{j.name}': {j.action_prompt}")
            time.sleep(1)


# ═══════════════════════════════════════════════════════════════════════
# 10. GATEWAY (s11) — simple HTTP server
# ═══════════════════════════════════════════════════════════════════════

_gateway_server: Optional[socketserver.TCPServer] = None
_gateway_thread: Optional[threading.Thread] = None

class GatewayHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "model": MODEL}).encode())
        elif self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": MODEL, "object": "model"}]}).encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            msgs = body.get("messages", [])
            stream = body.get("stream", False)
            try:
                resp = client.messages.create(model=MODEL, messages=msgs, max_tokens=4096)
                content = "".join(b.text for b in resp.content if b.type == "text")
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunk = json.dumps({"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]})
                    self.wfile.write(f"data: {chunk}\n\n".encode())
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, fmt, *args): pass  # suppress HTTP log spam


def start_gateway(port: int = 8900) -> bool:
    global _gateway_server, _gateway_thread
    if _gateway_server: return False
    _gateway_server = socketserver.TCPServer(("", port), GatewayHandler)
    _gateway_thread = threading.Thread(target=_gateway_server.serve_forever, daemon=True)
    _gateway_thread.start()
    return True

def stop_gateway():
    global _gateway_server, _gateway_thread
    if _gateway_server: _gateway_server.shutdown(); _gateway_server = None
    _gateway_thread = None


# ═══════════════════════════════════════════════════════════════════════
# 11. SUBAGENT (s13)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class SubagentResult:
    task_id: str; content: str; status: str = "completed"

class SubagentManager:
    def __init__(self):
        self._results: dict[str, SubagentResult] = {}
        self._pending: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def spawn(self, prompt: str) -> str:
        task_id = uuid.uuid4().hex[:8]
        thread = threading.Thread(target=self._run, args=(task_id, prompt), daemon=True)
        with self._lock: self._pending[task_id] = thread
        thread.start()
        return task_id

    def _run(self, task_id: str, prompt: str):
        try:
            resp = client.messages.create(
                model=MODEL,
                system="You are a subagent. Complete the task and report concisely.",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
            )
            content = "".join(b.text for b in resp.content if b.type == "text")
            result = SubagentResult(task_id, content.strip())
        except Exception as e:
            result = SubagentResult(task_id, f"Error: {e}", "error")
        with self._lock:
            self._results[task_id] = result; self._pending.pop(task_id, None)

    def collect(self, task_id: str) -> SubagentResult | None:
        with self._lock: return self._results.pop(task_id, None)

    def collect_all(self) -> list[SubagentResult]:
        with self._lock:
            results = list(self._results.values()); self._results.clear(); return results

    def pending_count(self) -> int:
        with self._lock: return len(self._pending)

    def wait_all(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._pending: return
            time.sleep(0.2)


# ═══════════════════════════════════════════════════════════════════════
# 12. DREAM (s14) — two-phase memory consolidation
# ═══════════════════════════════════════════════════════════════════════

class HistoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cursor_file = self.path.parent / ".dream_cursor"
    def append(self, entry: dict) -> int:
        entries = self._read_all(); cursor = (entries[-1]["cursor"] + 1) if entries else 1
        record = {"cursor": cursor, "timestamp": time.strftime("%Y-%m-%d %H:%M"), "content": entry.get("content", "")}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return cursor
    def read_since(self, cursor: int) -> list[dict]:
        return [e for e in self._read_all() if e.get("cursor", 0) > cursor]
    def _read_all(self) -> list[dict]:
        if not self.path.exists() or self.path.stat().st_size == 0: return []
        return [json.loads(l) for l in self.path.read_text().strip().splitlines() if l]
    def get_last_cursor(self) -> int:
        if self._cursor_file.exists():
            try: return int(self._cursor_file.read_text().strip())
            except ValueError: pass
        return 0
    def set_last_cursor(self, cursor: int):
        self._cursor_file.write_text(str(cursor), encoding="utf-8")

class DreamProcessor:
    def __init__(self, store: MemoryStore, history: HistoryStore):
        self.store = store; self.history = history

    def run(self) -> bool:
        last = self.history.get_last_cursor()
        entries = self.history.read_since(last)
        if not entries: return False
        text = "\n".join(f"[{e['timestamp']}] {e['content'][:500]}" for e in entries)
        try:
            resp = client.messages.create(
                model=MODEL,
                system="You are a memory consolidation agent (Phase 1). Analyze the conversation "
                       "and extract KEY FACTS, PATTERNS, and STALE info from current memory.\n\n"
                       f"Current memory:\n{self.store.to_string()}",
                messages=[{"role": "user", "content": text[:6000]}],
                max_tokens=1024,
            )
            analysis = "".join(b.text for b in resp.content if b.type == "text")
            if analysis:
                self.store.put(f"dream_{int(time.time())}", f"Dream analysis: {analysis[:500]}")
                self.history.set_last_cursor(entries[-1]["cursor"])
                return True
        except Exception:
            pass
        return False


# ═══════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT BUILDER
# ═══════════════════════════════════════════════════════════════════════

def build_system_prompt(registry, loader, memory, session, config, subagent_mgr):
    lines = [
        "You are OpenClaw, a full-stack AI agent harness.",
        "",
        "## Available Tools",
        json.dumps(registry.list_schemas(), indent=2),
        "",
    ]
    skills = loader.get_summary()
    if skills and skills != "No skills available.":
        lines.append("## Skills"); lines.append(skills); lines.append("")

    mem = memory.to_string()
    if mem != "(empty)":
        lines.append("## Memory"); lines.append(mem); lines.append("")

    if session:
        lines.append(f"## Session: {session.id} ({len(session.messages)} messages)"); lines.append("")
    if config:
        lines.append("## Configuration"); lines.append(json.dumps(config, indent=2)); lines.append("")
    pending = subagent_mgr.pending_count()
    if pending > 0:
        lines.append(f"## Background Subagents: {pending} running"); lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# AGENT LOOP (s01)
# ═══════════════════════════════════════════════════════════════════════

def agent_loop(messages, system_prompt, registry):
    while True:
        response = client.messages.create(
            model=MODEL, system=system_prompt, messages=messages,
            tools=registry.list_schemas(), max_tokens=4096,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            break
        if response.stop_reason == "tool_use":
            for block in response.content:
                if block.type == "tool_use":
                    result = registry.execute(block.name, **block.input)
                    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id, "content": result}]})
    return response.content


# ═══════════════════════════════════════════════════════════════════════
# MAIN REPL
# ═══════════════════════════════════════════════════════════════════════

def repl():
    print("=" * 60)
    print("  OpenClaw Capstone — 14 Sessions Integrated")
    print("  s01 Loop | s02 Tools | s03 Skills | s04 Memory")
    print("  s05 Ctx  | s06 Bus   | s07 Session| s08 Chans")
    print("  s09 Cfg  | s10 Cron  | s11 Gateway| s12 Security")
    print("  s13 Subagent | s14 Dream")
    print("=" * 60)

    # ── Init all subsystems ──────────────────────────────────────────

    registry = ToolRegistry()
    for t in [BashTool(), ReadTool(), WriteTool()]:
        registry.register(t)
    print(f"  [s02] Tools: {', '.join(registry.list_names())}")

    skills_dir = WORKDIR / "skills"
    skills_dir.mkdir(exist_ok=True)
    loader = SkillLoader(skills_dir)
    loader.discover()
    print(f"  [s03] Skills: {', '.join(loader.list_names()) or '(none)'}")

    memory = MemoryStore()
    print(f"  [s04] Memory ready")

    ctx_mgr = ContextManager(max_tokens=8000)
    print(f"  [s05] Context (max {ctx_mgr.max_tokens} tokens)")

    print(f"  [s06] Message Bus ready")

    sess_mgr = SessionManager(WORKDIR / "sessions_data")
    sess_mgr.load()
    session = sess_mgr.create()
    print(f"  [s07] Session: {session.id}")

    ch_mgr = ChannelManager()
    ch_mgr.register(CLIChannel()); ch_mgr.register(LogChannel())
    print(f"  [s08] Channels: {', '.join(ch_mgr.list_channels())}")

    config_path = WORKDIR / "config.json"
    config = ConfigLoader.load(config_path)
    print(f"  [s09] Config loaded")

    cron = CronService()
    cron.start()
    print(f"  [s10] Cron ready")

    print(f"  [s11] Gateway — type /serve to start")

    print(f"  [s12] Security — workspace restricted")

    sub_mgr = SubagentManager()
    print(f"  [s13] Subagent ready")

    history = HistoryStore(WORKDIR / ".dream" / "history.jsonl")
    dream = DreamProcessor(memory, history)
    print(f"  [s14] Dream ready ({history.path})")

    print()
    print("  Commands:")
    print("    /session <id>   — switch session")
    print("    /sessions       — list sessions")
    print("    /memory         — show memory entries")
    print("    /compact        — compact context")
    print("    /skills         — list skills")
    print("    /cron list|add  — manage cron jobs")
    print("    /spawn <prompt> — spawn subagent")
    print("    /collect [id]   — collect subagent results")
    print("    /serve [port]   — start HTTP gateway")
    print("    /dream          — run dream consolidation")
    print("    /config         — show config")
    print("    /status         — full status")
    print("    q               — quit")
    print()

    messages = session.messages

    while True:
        # Cron ticks
        tick = cron.get_tick()
        if tick:
            print(f"\n\033[91m{tick}\033[0m")
            messages.append({"role": "user", "content": tick})
            sp = build_system_prompt(registry, loader, memory, session, config, sub_mgr)
            content = agent_loop(messages, sp, registry)
            for b in content:
                if b.type == "text": ch_mgr.send_all(b.text)
            sess_mgr.save()

        try:
            user_input = input("\033[36ms_full >> \033[0m")
        except (EOFError, KeyboardInterrupt): break

        if user_input.lower() in ("q", "quit", "exit"): break

        # ── Commands ────────────────────────────────────────────────
        cmd = user_input.split()

        if user_input == "/sessions":
            for s in sess_mgr.list_sessions():
                active = " [active]" if s.id == sess_mgr._active_id else ""
                print(f"  {s.id}{active} ({len(s.messages)} messages)")
            continue

        if cmd and cmd[0] == "/session" and len(cmd) > 1:
            if sess_mgr.switch_to(cmd[1]):
                session = sess_mgr.get_active(); messages = session.messages if session else []
                print(f"  Switched to '{cmd[1]}'")
            else: print(f"  Session '{cmd[1]}' not found")
            continue

        if user_input == "/memory":
            print(memory.to_string() or "  (empty)")
            continue

        if user_input == "/compact":
            if ctx_mgr.should_compact(messages):
                old = len(messages); messages = ctx_mgr.compact(messages); session.messages = messages
                print(f"  Compacted: {old} -> {len(messages)}")
            else: print(f"  OK ({TokenCounter.count_messages(messages)} / {ctx_mgr.max_tokens} tokens)")
            continue

        if user_input == "/skills":
            for n in loader.list_names(): print(f"  - {n}")
            continue

        if user_input.startswith("/cron"):
            parts = user_input.split(maxsplit=2)
            if len(parts) >= 2 and parts[1] == "list":
                for j in cron.list_jobs(): print(f"  {j.name}: every {j.interval_seconds}s [{j.enabled and 'on' or 'off'}] -> {j.action_prompt}")
            elif len(parts) >= 3 and parts[1] == "add":
                try:
                    interval = CronParser.parse(parts[2].split(maxsplit=1)[0])
                    prompt_text = parts[2].split(maxsplit=1)[1] if " " in parts[2] else "tick"
                    cron.add_job(CronJob(name=f"job_{len(cron.list_jobs()) + 1}", interval_seconds=interval, action_prompt=prompt_text))
                    print("  Added cron job")
                except ValueError as e: print(f"  Error: {e}")
            else: print("  Usage: /cron list|add <interval> <prompt>")
            continue

        if cmd and cmd[0] == "/spawn":
            prompt = " ".join(cmd[1:])
            if prompt:
                tid = sub_mgr.spawn(prompt); print(f"  Spawned subagent: {tid}")
            else: print("  Usage: /spawn <prompt>")
            continue

        if user_input.startswith("/collect"):
            parts = user_input.split(maxsplit=1)
            tid = parts[1] if len(parts) > 1 else ""
            if tid:
                r = sub_mgr.collect(tid)
                if r: print(f"  [{r.task_id}] ({r.status}): {r.content[:500]}")
                else: print(f"  No result for '{tid}'")
            else:
                results = sub_mgr.collect_all()
                if results:
                    for r in results: print(f"  [{r.task_id}] ({r.status}): {r.content[:200]}")
                else: print(f"  No completed results. Pending: {sub_mgr.pending_count()}")
            continue

        if user_input == "/serve" or (cmd and cmd[0] == "/serve"):
            port = int(cmd[1]) if len(cmd) > 1 else 8900
            if start_gateway(port): print(f"  Gateway started on http://localhost:{port}")
            else: print("  Gateway already running")
            continue

        if user_input == "/dream":
            if dream.run(): print("  Dream: memory consolidated")
            else: print("  Dream: no new entries to process")
            continue

        if user_input == "/config":
            print(json.dumps(config, indent=2) if config else "  (empty)")
            continue

        if user_input == "/status":
            print(f"  Model: {MODEL}")
            print(f"  Tools: {', '.join(registry.list_names())}")
            print(f"  Skills: {', '.join(loader.list_names()) or '(none)'}")
            print(f"  Memory entries: {len(memory.list_keys())}")
            print(f"  Context: {TokenCounter.count_messages(messages)} / {ctx_mgr.max_tokens} tokens")
            print(f"  Messages: {len(messages)}")
            print(f"  Session: {session.id if session else '(none)'}")
            print(f"  Channels: {', '.join(ch_mgr.list_channels())}")
            print(f"  Cron jobs: {len(cron.list_jobs())}")
            print(f"  Subagents pending: {sub_mgr.pending_count()}")
            print(f"  Gateway: {'running' if _gateway_server else 'stopped'}")
            print(f"  Workspace: {WORKDIR}")
            print(f"  Security: workspace-restricted")
            continue

        # ── Normal message ──────────────────────────────────────────
        messages.append({"role": "user", "content": user_input})
        sp = build_system_prompt(registry, loader, memory, session, config, sub_mgr)
        content = agent_loop(messages, sp, registry)
        for b in content:
            if b.type == "text": ch_mgr.send_all(b.text)

        # Record to dream history
        history.append({"content": user_input[:200]})

        # Auto-compact
        if ctx_mgr.should_compact(messages):
            old = len(messages); messages = ctx_mgr.compact(messages); session.messages = messages
            print(f"\n  [Auto-compacted: {old} -> {len(messages)} messages]")

        sess_mgr.save()

    cron.stop()
    stop_gateway()
    sess_mgr.save()
    print("Shutdown complete.")


if __name__ == "__main__":
    repl()