#!/usr/bin/env python3
"""Harness layer: Session Management

    +-----------+
    | Sessions  |  JSONL files on disk
    | Directory |
    +-----------+
         |
    +-----------+
    | Session   |  CRUD operations
    | Manager   |  History retrieval
    +-----------+
         |
    +-----------+
    | Agent     |  Load/save per turn
    | Loop      |
    +-----------+

Key insight: A session is just a JSONL file. Each line is one
message pair (user + assistant). No database needed.
"""

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")

PROMPT_COLOR = "\033[36ms07 >> \033[0m"


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
# Session
# ---------------------------------------------------------------------------
@dataclass
class Session:
    """Represents a single conversation session."""

    session_id: str
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat() + "Z"
        if not self.updated_at:
            self.updated_at = self.created_at


# ---------------------------------------------------------------------------
# RuntimeCheckpoint — crash recovery
# ---------------------------------------------------------------------------
class RuntimeCheckpoint:
    """Saves and restores in-progress turn state for crash recovery.

    If the agent crashes mid-turn (power loss, process kill, network
    failure), the checkpoint preserves the partial conversation so it
    can be resumed on restart.

    Key concept: we save the assistant message and tool results that
    were completed *before* the crash. On restore, any pending tool
    calls are reported as "interrupted" so the model can decide what
    to do next.
    """

    CHECKPOINT_KEY = "runtime_checkpoint"
    PENDING_USER_KEY = "pending_user_turn"

    @staticmethod
    def save(session: Session, assistant_msg: dict,
             completed_results: list[dict] = None,
             pending_calls: list[dict] = None) -> None:
        """Persist the current turn state into session metadata."""
        session.metadata[RuntimeCheckpoint.CHECKPOINT_KEY] = {
            "assistant_message": assistant_msg,
            "completed_tool_results": completed_results or [],
            "pending_tool_calls": pending_calls or [],
        }

    @staticmethod
    def restore(session: Session, messages: list) -> bool:
        """Materialize an unfinished turn into the messages list.

        Returns True if a checkpoint was restored.
        """
        checkpoint = session.metadata.pop(RuntimeCheckpoint.CHECKPOINT_KEY, None)
        if not checkpoint:
            return False

        assistant_msg = checkpoint.get("assistant_message")
        completed = checkpoint.get("completed_tool_results", [])
        pending = checkpoint.get("pending_tool_calls", [])

        if assistant_msg:
            messages.append(assistant_msg)
        for result in completed:
            messages.append(result)
        for call in pending:
            # Report interrupted calls so the model knows
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": call.get("function", {}).get("name", "tool"),
                "content": "Error: Task interrupted before this tool finished.",
            })

        return True

    @staticmethod
    def mark_pending_user(session: Session) -> None:
        """Mark that a user message was persisted but not yet answered."""
        session.metadata[RuntimeCheckpoint.PENDING_USER_KEY] = True

    @staticmethod
    def clear_pending_user(session: Session) -> None:
        session.metadata.pop(RuntimeCheckpoint.PENDING_USER_KEY, None)

    @staticmethod
    def restore_pending_user(session: Session, messages: list) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        if not session.metadata.pop(RuntimeCheckpoint.PENDING_USER_KEY, None):
            return False
        if messages and messages[-1].get("role") == "user":
            messages.append({
                "role": "assistant",
                "content": "Error: Task interrupted before a response was generated.",
            })
        return True


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------
class SessionManager:
    """Manages sessions as JSONL files on disk.

    Each JSONL line is one JSON object {"role": "...", "content": "..."}.
    Sessions are stored in a directory named 'sessions' under the given base.
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def create(self) -> Session:
        """Create a new session and return it."""
        session_id = uuid.uuid4().hex[:12]
        session = Session(session_id=session_id)
        # Write an empty file to mark existence
        self._session_path(session_id).touch()
        return session

    def get(self, session_id: str) -> Optional[Session]:
        """Get a session by ID, or None if not found."""
        path = self._session_path(session_id)
        if not path.exists():
            return None
        # Read the file to count messages and extract metadata
        count = 0
        updated = None
        metadata = {}
        if path.stat().st_size > 0:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            if entry.get("_meta"):
                                metadata = entry["_meta"]
                            else:
                                count += 1
                                updated = entry.get("timestamp", "")
                        except json.JSONDecodeError:
                            continue
        created = datetime.fromtimestamp(path.stat().st_ctime).isoformat() + "Z"
        return Session(
            session_id=session_id,
            created_at=created,
            updated_at=updated or created,
            message_count=count,
            metadata=metadata,
        )

    def append(self, session_id: str, messages: list) -> None:
        """Append message entries to a session's JSONL file.

        Each entry: {"role": "...", "content": "...", "timestamp": "..."}
        """
        path = self._session_path(session_id)
        now = datetime.utcnow().isoformat() + "Z"
        with open(path, "a", encoding="utf-8") as f:
            for msg in messages:
                entry = {
                    "role": msg.get("role", "unknown"),
                    "content": msg.get("content", ""),
                    "timestamp": now,
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def save_metadata(self, session: Session) -> None:
        """Persist session metadata to the JSONL file.

        Uses a special _meta marker line so it doesn't interfere with
        regular message entries.
        """
        if not session.metadata:
            return
        path = self._session_path(session.session_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"_meta": session.metadata}, ensure_ascii=False) + "\n")

    def get_history(self, session_id: str, limit: int = 50) -> list:
        """Retrieve the most recent messages from a session."""
        path = self._session_path(session_id)
        if not path.exists():
            return []
        lines = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return lines[-limit:]

    def list_sessions(self) -> list[Session]:
        """List all sessions, sorted by most recent first."""
        sessions = []
        for fpath in sorted(self.sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            sid = fpath.stem
            sess = self.get(sid)
            if sess:
                sessions.append(sess)
        return sessions


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------
def repl():
    client = Anthropic()
    sess_mgr = SessionManager(WORKDIR / "sessions")

    tools = [
        _make_bash_tool(),
        _make_read_tool(),
        _make_write_tool(),
    ]
    api_tools = [t.to_api_dict() for t in tools]

    system_prompt = (
        "You are an AI assistant with session management. "
        "Your conversation is persisted to a JSONL file on disk. "
        "You can use bash, read, and write tools to interact with the filesystem."
    )

    # Auto-create a new session on start
    session = sess_mgr.create()
    messages = []  # current in-memory conversation for this session

    print(f"{PROMPT_COLOR}Session agent started.")
    print(f"{PROMPT_COLOR}Active session: {session.session_id} (created {session.created_at})")
    print(f"{PROMPT_COLOR}Commands:  /sessions      (list all sessions)")
    print(f"{PROMPT_COLOR}           /session <id>  (switch to a session)")
    print(f"{PROMPT_COLOR}           /save          (save current messages to session)")
    print(f"{PROMPT_COLOR}           /load          (load session history into memory)")
    print(f"{PROMPT_COLOR}           /info          (show current session info)")
    print(f"{PROMPT_COLOR}           q              (quit)")
    print()

    def load_session(sid: str):
        """Load a session's history into the messages list."""
        nonlocal session, messages
        sess = sess_mgr.get(sid)
        if sess is None:
            print(f"{PROMPT_COLOR}Session not found: {sid}")
            return False
        history = sess_mgr.get_history(sid, limit=50)
        session = sess
        messages = list(history)
        print(
            f"{PROMPT_COLOR}Switched to session {sess.session_id}"
            f" ({sess.message_count} messages loaded)"
        )
        return True

    def save_session():
        """Save current messages to the active session."""
        nonlocal session, messages
        if messages:
            sess_mgr.append(session.session_id, messages)
            saved = len(messages)
            print(f"{PROMPT_COLOR}Saved {saved} messages to session {session.session_id}")
            # Re-fetch to update count
            updated = sess_mgr.get(session.session_id)
            if updated:
                session = updated
        else:
            print(f"{PROMPT_COLOR}No messages to save.")

    while True:
        try:
            user_input = input(f"{PROMPT_COLOR}")
        except (EOFError, KeyboardInterrupt):
            print()
            # Save on exit
            if messages:
                sess_mgr.append(session.session_id, messages)
                print(f"{PROMPT_COLOR}Auto-saved {len(messages)} messages.")
            break

        raw = user_input.strip()

        if raw.lower() == "q":
            # Save current session before quitting
            if messages:
                sess_mgr.append(session.session_id, messages)
                print(f"{PROMPT_COLOR}Auto-saved {len(messages)} messages to {session.session_id}.")
            print(f"{PROMPT_COLOR}Goodbye.")
            break

        # -- REPL commands --

        if raw == "/sessions":
            all_sessions = sess_mgr.list_sessions()
            if all_sessions:
                print(f"{PROMPT_COLOR}--- Sessions ({len(all_sessions)}) ---")
                for s in all_sessions:
                    marker = " <-- active" if s.session_id == session.session_id else ""
                    print(
                        f"{PROMPT_COLOR}  {s.session_id}  "
                        f"({s.message_count} msgs, {s.updated_at[:19]}){marker}"
                    )
            else:
                print(f"{PROMPT_COLOR}No sessions found.")
            continue

        if raw.startswith("/session "):
            sid = raw[len("/session "):].strip()
            # Save current session first
            if messages:
                sess_mgr.append(session.session_id, messages)
                print(
                    f"{PROMPT_COLOR}Saved {len(messages)} messages to current session"
                    f" {session.session_id}."
                )
                messages = []
            load_session(sid)
            continue

        if raw == "/save":
            save_session()
            continue

        if raw == "/load":
            history = sess_mgr.get_history(session.session_id, limit=50)
            messages = list(history)
            print(
                f"{PROMPT_COLOR}Loaded {len(messages)} messages from session"
                f" {session.session_id}."
            )
            continue

        if raw == "/info":
            print(
                f"{PROMPT_COLOR}Session: {session.session_id}"
                f"\n{PROMPT_COLOR}  Created:  {session.created_at[:19]}"
                f"\n{PROMPT_COLOR}  Updated:  {session.updated_at[:19]}"
                f"\n{PROMPT_COLOR}  Messages: {session.message_count}"
                f"\n{PROMPT_COLOR}  In-memory: {len(messages)}"
            )
            continue

        # -- Normal conversation --
        messages.append({"role": "user", "content": raw})

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
                messages.append({"role": "assistant", "content": response.model_dump()["content"]})
                break

            else:
                print(f"{PROMPT_COLOR}Unexpected stop_reason: {response.stop_reason}")
                break

        # Auto-save after each turn
        sess_mgr.append(session.session_id, messages[-2:])  # save just the latest pair
        session = sess_mgr.get(session.session_id) or session


if __name__ == "__main__":
    repl()