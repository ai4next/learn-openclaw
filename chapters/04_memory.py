#!/usr/bin/env python3
"""Harness layer: Memory System

    +-----------+
    | MEMORY.md |  Persistent long-term memory file
    +-----------+
         |
    +-----------+
    | Memory    |  Read/write/append operations
    | Store     |
    +-----------+
         |
    +-----------+
    | Consoli-  |  Token-budget driven LLM summarization
    | dator     |  of old messages -> history.jsonl
    +-----------+
         |
    +-----------+
    | System    |  "Current memory: ..."
    | Prompt    |
    +-----------+

Key insight: Memory is just a file. The agent reads it at startup
and writes to it during conversation. No vector DB needed.

The Consolidator extends this: when the conversation grows too large,
old messages are summarized via LLM and archived to history.jsonl.
This keeps the context window clean without losing the gist.
"""

import json
import os
import sys
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")

PROMPT_COLOR = "\033[36ms04 >> \033[0m"


# ---------------------------------------------------------------------------
# Simple token estimator (4 chars per token heuristic)
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for most models."""
    return len(text) // 4

def estimate_messages_tokens(messages: list) -> int:
    """Estimate total tokens in a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content) + 8  # overhead
        elif isinstance(content, list):
            for block in content:
                text = block.get("text", "") if isinstance(block, dict) else str(block)
                total += estimate_tokens(str(text)) + 4
    return total


# ---------------------------------------------------------------------------
# Tool base
# ---------------------------------------------------------------------------
class Tool:
    """A tool the agent may call. name + description + input_schema are sent
    to the API; the `execute` callback runs the actual logic."""

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
# Built-in tools (bash, read, write)
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
        description="Execute a bash command (non-interactive, 60s timeout). CWD is the harness directory.",
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
# MemoryStore
# ---------------------------------------------------------------------------
class MemoryStore:
    """Manages a persistent MEMORY.md file on disk."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("# Agent Memory\n\n(No memories yet.)\n", encoding="utf-8")

    def read(self) -> str:
        """Read the current memory file."""
        return self.path.read_text(encoding="utf-8")

    def write(self, content: str) -> None:
        """Overwrite the memory file entirely."""
        self.path.write_text(content, encoding="utf-8")

    def append(self, text: str) -> None:
        """Append a new memory entry."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"\n{text}\n")


# ---------------------------------------------------------------------------
# Consolidator — token-budget driven memory compression
# ---------------------------------------------------------------------------
class Consolidator:
    """Summarizes old conversation turns via LLM and archives to history.jsonl.

    When the conversation exceeds the token budget, the consolidator picks
    the oldest user-turn boundary, sends it to the LLM for summarization,
    and writes the summary to history.jsonl. The original messages are then
    removed from the active conversation.
    """

    def __init__(self, archive_dir: Path, api_key: str = None):
        self.archive_dir = archive_dir
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.archive_dir / "history.jsonl"
        self.client = Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))

    def _format_chunk(self, messages: list) -> str:
        """Format messages as text for the LLM summarizer."""
        lines = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
            ts = m.get("timestamp", "")
            prefix = f"[{ts}] {role.upper()}" if ts else f"{role.upper()}"
            lines.append(f"{prefix}: {content[:2000]}")
        return "\n\n".join(lines)

    def archive(self, messages: list) -> str | None:
        """Summarize a chunk of messages via LLM and append to history.jsonl.

        Returns the summary text, or None if nothing was archived.
        """
        if not messages:
            return None

        formatted = self._format_chunk(messages)
        summary = self._summarize(formatted)
        if summary:
            record = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": "consolidation",
                "summary": summary,
                "original_count": len(messages),
            }
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return summary

    def raw_archive(self, messages: list) -> None:
        """Fallback: dump messages directly without LLM summarization."""
        record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "raw_archive",
            "summary": self._format_chunk(messages)[:8000],
            "original_count": len(messages),
        }
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _summarize(self, text: str) -> str | None:
        """Call the LLM to produce a concise summary of the text."""
        try:
            response = self.client.messages.create(
                model=MODEL,
                system="You are a memory consolidation agent. Summarize the following "
                       "conversation turns into a concise paragraph. Preserve key facts, "
                       "decisions, and user preferences. Omit tool implementation details.",
                messages=[{"role": "user", "content": text}],
                max_tokens=1024,
            )
            summary = "".join(b.text for b in response.content if b.type == "text")
            return summary.strip() or None
        except Exception:
            return None

    def pick_boundary(self, messages: list, max_tokens: int) -> int | None:
        """Find the oldest user-turn boundary where accumulated tokens exceed max_tokens."""
        accumulated = 0
        for idx, msg in enumerate(messages):
            if idx > 0 and msg.get("role") == "user" and accumulated >= max_tokens:
                return idx
            content = msg.get("content", "")
            if isinstance(content, str):
                accumulated += estimate_tokens(content) + 8
            elif isinstance(content, list):
                for block in content:
                    text = block.get("text", "") if isinstance(block, dict) else ""
                    accumulated += estimate_tokens(text) + 4
        return None

    def maybe_consolidate(self, messages: list, budget: int) -> tuple[list, str | None]:
        """If messages exceed budget, consolidate the oldest portion.

        Returns (truncated_messages, summary_of_removed).
        """
        current = estimate_messages_tokens(messages)
        if current <= budget:
            return messages, None

        boundary = self.pick_boundary(messages, current - budget)
        if boundary is None or boundary < 2:
            return messages, None

        chunk = messages[:boundary]
        remaining = messages[boundary:]

        summary = self.archive(chunk)
        if summary is None:
            self.raw_archive(chunk)

        return remaining, summary


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------
class MemoryManager:
    """Wraps a MemoryStore and exposes tool definitions for the agent."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def read_memory_tool(self) -> Tool:
        def execute(**kwargs):
            return self.store.read()

        return Tool(
            name="read_memory",
            description="Read the persistent memory (MEMORY.md). Contains information remembered from past sessions.",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            execute=execute,
        )

    def write_memory_tool(self) -> Tool:
        def execute(**kwargs):
            content = kwargs.get("content", "")
            mode = kwargs.get("mode", "write")
            if mode == "append":
                self.store.append(content)
            else:
                self.store.write(content)
            return f"Memory {mode} successful ({len(content)} chars)."

        return Tool(
            name="write_memory",
            description="Write or append to persistent memory. Use 'write' to overwrite, 'append' to add.",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content to store.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["write", "append"],
                        "description": "'write' overwrites; 'append' adds a new entry.",
                    },
                },
                "required": ["content"],
            },
            execute=execute,
        )


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def repl():
    client = Anthropic()

    # Memory setup
    memory_dir = WORKDIR / ".memory"
    store = MemoryStore(memory_dir / "MEMORY.md")
    mem_mgr = MemoryManager(store)

    # Consolidator setup
    consolidator = Consolidator(archive_dir=memory_dir)
    CONSOLIDATION_BUDGET = 40_000  # tokens: trigger consolidation above this

    tools = [
        _make_bash_tool(),
        _make_read_tool(),
        _make_write_tool(),
        mem_mgr.read_memory_tool(),
        mem_mgr.write_memory_tool(),
    ]
    api_tools = [t.to_api_dict() for t in tools]

    system_prompt = (
        "You are an AI assistant with persistent memory. "
        "You can read and write to a memory file that persists across sessions.\n\n"
        "## Current Memory\n"
        f"{store.read()}\n\n"
        "Use the read_memory and write_memory tools to manage your long-term memory. "
        "The memory file is MEMORY.md and lives inside the .memory directory."
    )

    messages = []

    print(f"{PROMPT_COLOR}Memory agent started. Memory file: {store.path}")
    print(f"{PROMPT_COLOR}Consolidator archive: {consolidator.history_file}")
    print(f"{PROMPT_COLOR}Consolidation budget: {CONSOLIDATION_BUDGET} tokens")
    print(f"{PROMPT_COLOR}Type 'q' to quit.")

    while True:
        try:
            user_input = input(f"{PROMPT_COLOR}")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input.strip().lower() == "q":
            print(f"{PROMPT_COLOR}Goodbye.")
            break

        messages.append({"role": "user", "content": user_input})

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

                # After tool results, check if consolidation is needed
                messages, summary = consolidator.maybe_consolidate(messages, CONSOLIDATION_BUDGET)
                if summary:
                    print(f"{PROMPT_COLOR}[Consolidator] Archived old messages. Summary: {summary[:120]}...")

                continue  # let the model see tool results

            elif response.stop_reason == "end_turn":
                for block in response.content:
                    if block.type == "text":
                        print(f"{PROMPT_COLOR}{block.text}")
                messages.append({"role": "assistant", "content": response.model_dump()["content"]})

                # Check consolidation after end_turn too
                messages, summary = consolidator.maybe_consolidate(messages, CONSOLIDATION_BUDGET)
                if summary:
                    print(f"{PROMPT_COLOR}[Consolidator] Archived old messages. Summary: {summary[:120]}...")
                break

            else:
                print(f"{PROMPT_COLOR}Unexpected stop_reason: {response.stop_reason}")
                break


if __name__ == "__main__":
    repl()