# s14: Dream Memory Processor


> **"Dream is an agent that manages the agent's memory."**
>
> Harness layer: Meta-Cognition

## Problem

Memory files grow stale. Facts change, preferences evolve, old project status becomes irrelevant. With `read_memory` and `write_memory` tools (s05), the agent can manage its own memory — but only when actively thinking about it. What about the memories it didn't notice, or the patterns it forgot to record?

The **Dream** processor is a background agent that periodically reviews conversation history and updates memory files autonomously. It runs on a cron schedule (s11), analyzes new history entries, and makes targeted edits to `MEMORY.md`, `SOUL.md`, and `USER.md`.

## Solution

```
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
```

Two-phase design separates analysis from action. Phase 1 is a plain LLM call with no tools — it just reads and thinks. Phase 2 is a full AgentRunner with filesystem tools — it reads current files, compares with analysis, and makes targeted edits.

## How It Works

### 1. history.jsonl — the raw material

Dream processes entries from an append-only JSONL log. Each entry has a cursor, timestamp, and content:

```json
{"cursor": 1, "timestamp": "2026-05-11 10:30", "content": "User said they prefer Rust for systems programming"}
```

### 2. Phase 1 — Analyze

The LLM reads new entries since the last processed cursor and produces an analysis:

```python
def _phase1_analyze(self, history_text):
    response = self.client.messages.create(
        model=self.model,
        system="""You are a memory consolidation agent (Phase 1).
        Analyze the conversation history and extract:
        1. KEY FACTS: user preferences, decisions, personal details
        2. PATTERNS: recurring topics, behaviors, workflow patterns
        3. STALE: anything in current memory that contradicts new info
        4. SKILLS: any task patterns worth saving as a skill""",
        messages=[{"role": "user", "content": history_text}],
        max_tokens=2048,
    )
    return response.content
```

### 3. Phase 2 — Edit

The analysis drives file edits. Dream's Phase 2 has `read_file` and `edit_file` tools and uses the AgentRunner to make targeted updates:

```python
def _phase2_edit(self, analysis):
    current = self.store.read_memory()
    updated = current + "\n\n## Dream Update\n" + analysis[:2000]
    self.store.write_memory(updated)
```

### 4. Cursor tracking

Dream maintains a cursor so it only processes new entries:

```python
last_cursor = self.history.get_last_cursor()
entries = self.history.read_since(last_cursor)
# ... process ...
self.history.set_last_cursor(entries[-1]["cursor"])
```

## What Changed From s14

| Component | Before (s13) | After (s14) |
|-----------|--------------|-------------|
| Memory management | Manual via tools | Autonomous background processing |
| History format | In-memory only | Append-only `history.jsonl` with cursor |
| Processing trigger | User command | Cron-scheduled (or manual `/run`) |
| Analysis depth | None | Two-phase: analyze then edit |
| Cursor tracking | N/A | `.dream_cursor` file for incremental processing |

## Try It

```bash
python chapters/15_dream.py
```

Commands:

- `/add "User mentioned they prefer dark mode for the UI"` — simulate a conversation entry
- `/add "User decided to use PostgreSQL over MongoDB"` — another entry
- `/run` — run Dream to process entries
- `/memory` — see updated MEMORY.md
- `/cursor` — check last processed cursor

## Key Design Decisions

1. **Two-phase separation.** Phase 1 has no tools — it just analyzes. Phase 2 has tools but is guided by Phase 1's analysis. This prevents the agent from making premature edits.

2. **Append-only history.** The JSONL log is never modified — only appended to. This makes it safe for concurrent writers and crash-proof.

3. **Cursor-based incremental processing.** Dream only looks at new entries. This keeps each run fast and idempotent.
