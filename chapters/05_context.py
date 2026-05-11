#!/usr/bin/env python3
"""Harness layer: Context Management

    +-----------+
    | Token     |  Count tokens, track budget
    | Counter   |
    +-----------+
         |
    +-----------+
    | Auto-     |  Compact old tool results
    | Compact   |  Summarize and prune
    +-----------+
         |
    +-----------+
    | History   |  Slice to fit context
    | Pruner    |
    +-----------+

Key insight: Context windows are finite. The harness must
actively manage what fits — not silently truncate.
"""

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")

PROMPT_COLOR = "\033[36ms05 >> \033[0m"


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
# TokenCounter
# ---------------------------------------------------------------------------
class TokenCounter:
    """Rough token estimation: ~4 characters per token.

    Supports both string and list-of-block content formats.
    """

    CHARS_PER_TOKEN = 4

    @classmethod
    def count(cls, text: str) -> int:
        """Estimate token count for a string."""
        return max(1, len(text) // cls.CHARS_PER_TOKEN)

    @classmethod
    def count_messages(cls, messages: list) -> int:
        """Estimate total tokens across a list of messages.

        Handles both string content and list-of-block content (tool_use,
        tool_result, image_url blocks).
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = (
                            block.get("content", "")
                            or block.get("text", "")
                            or block.get("input", "")
                            or ""
                        )
                        # tool_use blocks have a nested function.input
                        if isinstance(text, dict):
                            text = json.dumps(text)
                        total += cls.count(str(text))
                        total += 2  # block overhead
                    else:
                        total += cls.count(str(block))
            else:
                total += cls.count(str(content))
            # Add overhead per message (~4 tokens for role + formatting)
            total += 4
        return total


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------
class ContextManager:
    """Manages the context budget: compacts and prunes message history."""

    def __init__(self, max_tokens: int = 100000):
        self.max_tokens = max_tokens

    def budget_remaining(self, messages: list) -> int:
        """Return how many tokens remain in the budget."""
        used = TokenCounter.count_messages(messages)
        return self.max_tokens - used

    def compact_messages(self, messages: list) -> list:
        """Replace old tool results with short summaries to save tokens.

        For each tool_result message older than the most recent 4 turns,
        replace the content with a one-line summary.
        """
        if len(messages) <= 8:
            return list(messages)

        compacted = []
        # Keep the most recent 4 user+assistant pairs (8 messages) intact
        recent = messages[-8:]
        older = messages[:-8]

        summary_count = 0
        for msg in older:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Compact tool_result messages
            if role == "user" and isinstance(content, list):
                new_content = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        orig = str(block.get("content", ""))
                        tokens_saved = TokenCounter.count(orig)
                        summary = f"[Compacted: was ~{tokens_saved} tokens]"
                        new_content.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("tool_use_id", ""),
                                "content": summary,
                            }
                        )
                        summary_count += 1
                    else:
                        new_content.append(block)
                compacted.append({**msg, "content": new_content})
            else:
                compacted.append(msg)

        compacted.extend(recent)
        return compacted

    def prune_history(self, messages: list, max_tokens: int = 80000) -> list:
        """Drop old messages until the history fits within max_tokens.

        Always keeps the most recent message pair.
        """
        if TokenCounter.count_messages(messages) <= max_tokens:
            return list(messages)

        pruned = list(messages)
        while TokenCounter.count_messages(pruned) > max_tokens and len(pruned) > 2:
            # Drop the oldest message
            pruned.pop(0)

        return pruned


# ---------------------------------------------------------------------------
# AutoCompact — TTL-based session expiry
# ---------------------------------------------------------------------------
class AutoCompact:
    """Automatically detects and compacts sessions that have been idle
    past their TTL (time-to-live).

    This runs in the background (e.g., between turns) to catch
    sessions that the user abandoned mid-conversation. When a session
    expires, its most recent context is preserved but older turns are
    flagged for compaction on the next active turn.
    """

    def __init__(self, ttl_minutes: int = 30):
        self.ttl_minutes = ttl_minutes
        self._last_active: float = 0.0

    def check_expired(self, session_messages: list) -> bool:
        """Check if the session has been idle past its TTL.

        Returns True if expired (caller should compact).
        """
        import time
        now = time.time()
        elapsed = now - self._last_active
        return elapsed > self.ttl_minutes * 60

    def mark_active(self) -> None:
        """Mark the session as active (reset the TTL timer)."""
        import time
        self._last_active = time.time()

    def auto_compact_if_expired(
        self,
        messages: list,
        context_mgr: ContextManager,
    ) -> tuple[list, bool]:
        """If the session is expired, compact and prune it.

        Returns (messages, was_compacted).
        """
        if not self.check_expired(messages):
            return messages, False

        before = TokenCounter.count_messages(messages)
        messages = context_mgr.compact_messages(messages)
        messages = context_mgr.prune_history(messages, max_tokens=60000)
        after = TokenCounter.count_messages(messages)
        self.mark_active()

        return messages, True


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------
def repl():
    client = Anthropic()
    ctx_mgr = ContextManager(max_tokens=100000)
    auto_compact = AutoCompact(ttl_minutes=30)

    tools = [
        _make_bash_tool(),
        _make_read_tool(),
        _make_write_tool(),
    ]
    api_tools = [t.to_api_dict() for t in tools]

    system_prompt = (
        "You are an AI assistant with context management. "
        "The harness manages token budgets and may compact old tool results automatically. "
        "You can use bash, read, and write tools to interact with the filesystem."
    )

    messages = []

    print(f"{PROMPT_COLOR}Context management agent started.")
    print(f"{PROMPT_COLOR}Commands:  /compact  (compact old tool results)")
    print(f"{PROMPT_COLOR}           /budget   (show token budget)")
    print(f"{PROMPT_COLOR}           /prune    (prune history to fit)")
    print(f"{PROMPT_COLOR}           /expire   (simulate TTL expiry to trigger AutoCompact)")
    print(f"{PROMPT_COLOR}           q         (quit)")

    while True:
        # AutoCompact: check expiry between turns
        messages, was_compacted = auto_compact.auto_compact_if_expired(messages, ctx_mgr)
        if was_compacted:
            print(f"{PROMPT_COLOR}[AutoCompact] Session was idle — compacted automatically.")

        try:
            user_input = input(f"{PROMPT_COLOR}")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        raw = user_input.strip()

        if raw.lower() == "q":
            print(f"{PROMPT_COLOR}Goodbye.")
            break

        # ---- REPL commands ----
        if raw == "/compact":
            before = TokenCounter.count_messages(messages)
            messages = ctx_mgr.compact_messages(messages)
            after = TokenCounter.count_messages(messages)
            saved = before - after
            print(
                f"{PROMPT_COLOR}Compacted: {before} -> {after} tokens"
                f" ({saved} saved, {len(messages)} messages)"
            )
            continue

        if raw == "/budget":
            used = TokenCounter.count_messages(messages)
            remaining = ctx_mgr.max_tokens - used
            print(
                f"{PROMPT_COLOR}Token budget: {used} used / {ctx_mgr.max_tokens} max"
                f" ({remaining} remaining)"
            )
            continue

        if raw == "/prune":
            before = TokenCounter.count_messages(messages)
            messages = ctx_mgr.prune_history(messages, max_tokens=80000)
            after = TokenCounter.count_messages(messages)
            print(
                f"{PROMPT_COLOR}Pruned: {before} -> {after} tokens"
                f" ({len(messages)} messages remain)"
            )
            continue

        if raw == "/expire":
            import time
            auto_compact._last_active = 0.0  # Force expiry
            messages, was_compacted = auto_compact.auto_compact_if_expired(messages, ctx_mgr)
            if was_compacted:
                print(f"{PROMPT_COLOR}[AutoCompact] Session compacted after simulated expiry.")
            else:
                print(f"{PROMPT_COLOR}[AutoCompact] No compaction needed.")
            continue

        # Normal conversation — mark active
        auto_compact.mark_active()
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


if __name__ == "__main__":
    repl()