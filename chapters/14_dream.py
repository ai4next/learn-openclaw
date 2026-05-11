#!/usr/bin/env python3
"""Harness layer: Dream Memory Processor

    +-----------+
    | history.  |  Append-only log of conversation chunks
    | jsonl     |
    +-----------+
         |
    +-----------+
    | Phase 1   |  LLM analyzes history: extract facts,
    | Analyze   |  detect patterns, flag stale content
    +-----------+
         |
    +-----------+
    | Phase 2   |  AgentRunner edits MEMORY.md, SOUL.md,
    | Edit      |  USER.md with read_file / edit_file
    +-----------+

Key insight: Dream is a background memory processor. It runs
periodically (via cron), reads new history entries, and decides
what to remember, update, or forget. It is an agent that manages
the agent's memory — a meta-cognitive layer.

Two-phase design:
  Phase 1: Plain LLM call — analyze new history entries.
  Phase 2: AgentRunner with filesystem tools — make targeted edits.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
PROMPT_COLOR = "\033[36ms14 >> \033[0m"


# ---------------------------------------------------------------------------
# History store
# ---------------------------------------------------------------------------
class HistoryStore:
    """Append-only JSONL log of conversation history entries."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        self._cursor_file = self.path.parent / ".dream_cursor"

    def append(self, entry: dict) -> int:
        """Append an entry and return its cursor."""
        entries = self._read_all()
        cursor = (entries[-1]["cursor"] + 1) if entries else 1
        record = {
            "cursor": cursor,
            "timestamp": time.strftime("%Y-%m-%d %H:%M"),
            "content": entry.get("content", ""),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def read_since(self, cursor: int) -> list[dict]:
        """Return entries newer than cursor."""
        return [e for e in self._read_all() if e.get("cursor", 0) > cursor]

    def _read_all(self) -> list[dict]:
        entries = []
        if not self.path.exists() or self.path.stat().st_size == 0:
            return entries
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def get_last_cursor(self) -> int:
        if self._cursor_file.exists():
            try:
                return int(self._cursor_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pass
        return 0

    def set_last_cursor(self, cursor: int) -> None:
        self._cursor_file.write_text(str(cursor), encoding="utf-8")


# ---------------------------------------------------------------------------
# MemoryStore (simple file-based store)
# ---------------------------------------------------------------------------
class MemoryStore:
    """Read/write MEMORY.md, SOUL.md, USER.md files."""

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.soul_file = memory_dir / "SOUL.md"
        self.user_file = memory_dir / "USER.md"

        # Initialize files if missing
        if not self.memory_file.exists():
            self.memory_file.write_text("# Agent Memory\n\n(No memories yet.)\n", encoding="utf-8")
        if not self.soul_file.exists():
            self.soul_file.write_text("# SOUL\n\nAgent personality and core behaviors.\n", encoding="utf-8")
        if not self.user_file.exists():
            self.user_file.write_text("# USER\n\nUser profile and preferences.\n", encoding="utf-8")

    def read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "(empty)"

    def write(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def read_memory(self) -> str:
        return self.read(self.memory_file)

    def write_memory(self, content: str) -> None:
        self.write(self.memory_file, content)


# ---------------------------------------------------------------------------
# Dream — Two-phase memory processor
# ---------------------------------------------------------------------------
class Dream:
    """Two-phase background memory consolidation.

    Phase 1: Analyze new history entries via LLM.
    Phase 2: Edit MEMORY.md, SOUL.md, USER.md based on analysis.
    """

    def __init__(self, store: MemoryStore, history: HistoryStore, client, model: str):
        self.store = store
        self.history = history
        self.client = client
        self.model = model

    def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        last_cursor = self.history.get_last_cursor()
        entries = self.history.read_since(last_cursor)
        if not entries:
            return False

        print(f"  Dream: Processing {len(entries)} new entries (cursor {last_cursor}+)")

        # Phase 1: Analyze
        history_text = "\n".join(
            f"[{e['timestamp']}] {e['content'][:500]}" for e in entries
        )
        analysis = self._phase1_analyze(history_text)
        if not analysis:
            print("  Dream Phase 1: No analysis produced.")
            return False

        print(f"  Dream Phase 1: Analysis produced ({len(analysis)} chars)")

        # Phase 2: Edit
        edits = self._phase2_edit(analysis)
        if edits:
            for edit in edits:
                print(f"  Dream Phase 2: {edit}")

        # Advance cursor
        self.history.set_last_cursor(entries[-1]["cursor"])
        return True

    def _phase1_analyze(self, history_text: str) -> str | None:
        """Phase 1: LLM analyzes conversation history for facts and patterns."""
        current_memory = self.store.read_memory()

        try:
            response = self.client.messages.create(
                model=self.model,
                system=(
                    "You are a memory consolidation agent (Phase 1). "
                    "Analyze the conversation history below and extract:\n"
                    "1. KEY FACTS: user preferences, decisions, personal details\n"
                    "2. PATTERNS: recurring topics, behaviors, workflow patterns\n"
                    "3. STALE: anything in current memory that contradicts new info\n"
                    "4. SKILLS: any task patterns worth saving as a skill\n\n"
                    "Current memory:\n" + current_memory[:2000]
                ),
                messages=[{"role": "user", "content": history_text[:8000]}],
                max_tokens=2048,
            )
            return "".join(b.text for b in response.content if b.type == "text")
        except Exception as e:
            print(f"  Dream Phase 1 error: {e}")
            return None

    def _phase2_edit(self, analysis: str) -> list[str]:
        """Phase 2: Use analysis to update memory files."""
        edits = []

        # If the analysis suggests memory updates, append them
        if "KEY FACTS" in analysis or "PATTERNS" in analysis:
            current = self.store.read_memory()
            updated = current + "\n\n## Dream Update\n" + analysis[:2000]
            self.store.write_memory(updated)
            edits.append("updated MEMORY.md")

        return edits


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def repl():
    client = Anthropic()

    # Storage
    dream_dir = WORKDIR / ".dream"
    store = MemoryStore(dream_dir)
    history = HistoryStore(dream_dir / "history.jsonl")

    # Dream processor
    dream = Dream(store, history, client, MODEL)

    print("=== OpenClaw s14: Dream Memory Processor ===")
    print("Two-phase background memory consolidation.")
    print(f"Memory: {store.memory_file}")
    print(f"History: {history.path}")
    print("Commands:")
    print("  /add <text>         - add a history entry for Dream to process")
    print("  /run                - run Dream processor")
    print("  /memory             - show current MEMORY.md")
    print("  /history            - show history entries")
    print("  /cursor             - show last processed cursor")
    print("  q                   - quit\n")

    while True:
        try:
            user_input = input(PROMPT_COLOR)
        except (EOFError, KeyboardInterrupt):
            break

        raw = user_input.strip()

        if raw.lower() in ("q", "quit", "exit"):
            print("Goodbye.")
            break

        if raw.startswith("/add "):
            text = raw[5:].strip()
            cursor = history.append({"content": text})
            print(f"  Added entry (cursor={cursor})")
            continue

        if raw == "/run":
            worked = dream.run()
            if worked:
                print("  Dream processing complete.")
            else:
                print("  No new entries to process.")
            continue

        if raw == "/memory":
            print(f"  MEMORY.md ({store.memory_file}):")
            print(f"  {store.read_memory()[:1000]}")
            continue

        if raw == "/history":
            entries = history._read_all()
            if not entries:
                print("  No history entries.")
            else:
                for e in entries[-10:]:
                    print(f"  [{e['cursor']}] {e['timestamp']}: {e['content'][:80]}")
            continue

        if raw == "/cursor":
            print(f"  Last processed: {history.get_last_cursor()}")
            continue

        print(f"  Unknown command. Try /add, /run, /memory, /history, /cursor")


if __name__ == "__main__":
    repl()