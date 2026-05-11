# s04: Memory System


> **"Memory is just a file. No vector DB needed."**
>
> Harness layer: Persistence

## Problem

Each session starts with a blank conversation. The agent has no memory of previous interactions -- names the user mentioned, decisions made, preferences expressed, work completed. For a tool-building assistant or a long-running project companion, this amnesia means the user must repeat context every session.

Enterprise solutions reach for vector databases, embedding pipelines, and retrieval-augmented generation (RAG). But for many use cases, these are overkill. The solution is **file-based memory**: persistent, human-readable markdown files that the agent reads at startup and updates during the session. Memory is just a file. The agent writes what it wants to remember; it reads what has been remembered.

## Solution

```
   Session Start          During Session              Next Session
       |                       |                          |
       v                       v                          v
+--------------+       +----------------+          +--------------+
| Read memory  |       | Agent writes   |          | Read memory  |
| files from   |       | updates to     |          | files from   |
| disk         |       | memory files   |          | disk         |
+--------------+       +----------------+          +--------------+
       |                       |                          |
       v                       v                          v
+--------------+       +----------------+          +--------------+
| Inject into  |       | Agent reads    |          | Inject into  |
| system       |       | memory for     |          | system       |
| prompt       |       | context        |          | prompt       |
+--------------+       +----------------+          +--------------+
```

Memory is stored as flat markdown files in a `memory/` directory. Each file represents a topic area (user preferences, project notes, decisions). The agent has a `read_memory` and `append_memory` tool to access and update these files. At session start, the agent reads all memory files and includes them in its context.

## How It Works

### 1. Define the memory directory structure

Memory files live under a `memory/` directory in the working directory:

```
memory/
  user-preferences.md    # User's name, preferred style, settings
  project-status.md      # Current project state, completed tasks
  decisions.md           # Architectural decisions and rationale
  learnings.md           # Things the agent discovered
```

Each file is plain Markdown. The agent writes these files; the harness reads them at startup.

### 2. Add memory tools

Two new tools extend the tool system:

- **`read_memory_dir`** -- Lists available memory files and returns their contents
- **`append_memory`** -- Appends a new entry to a memory file (or creates it)

```python
class ReadMemoryTool(Tool):
    @property
    def name(self): return "read_memory"

    @property
    def description(self): return "Read the contents of all memory files"

    def execute(self) -> str:
        memory_dir = WORKDIR / "memory"
        if not memory_dir.exists():
            return "No memory files found."
        output = []
        for path in sorted(memory_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            output.append(f"# {path.stem}\n{content}")
        return "\n\n---\n\n".join(output)


class AppendMemoryTool(Tool):
    @property
    def name(self): return "append_memory"

    @property
    def description(self): return "Append a new entry to a memory file"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Memory topic / filename stem (e.g. 'user-preferences')",
                },
                "entry": {
                    "type": "string",
                    "description": "Content to append",
                },
            },
            "required": ["topic", "entry"],
        }

    def execute(self, topic: str, entry: str) -> str:
        memory_dir = WORKDIR / "memory"
        memory_dir.mkdir(exist_ok=True)
        path = memory_dir / f"{topic}.md"
        timestamp = datetime.now().isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## {timestamp}\n{entry}\n")
        return f"Appended to {topic}.md"
```

### 3. Inject memory into the system prompt at startup

When the agent starts, the harness reads all memory files and includes them in the system prompt or as an initial tool result:

```python
@dataclass
class MemoryState:
    facts: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)

class MemoryManager:
    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(exist_ok=True)

    def load_all(self) -> str:
        """Load all memory files into a formatted string."""
        sections = []
        for path in sorted(self.memory_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            sections.append(f"=== {path.stem} ===\n{content}")
        return "\n\n".join(sections) if sections else "No prior memory."

    def consolidate(self, topic: str = None):
        """Consolidate a memory file: deduplicate, summarize (future)."""
        # For now, consolidation is a no-op.
        # In a future iteration, this could call the LLM to summarize.
        pass
```

The agent loop loads memory before the first user turn:

```python
memory_manager = MemoryManager(WORKDIR / "memory")
memory_content = memory_manager.load_all()

SYSTEM_PROMPT = f"""You are an OpenClaw agent with persistent memory.

Previous session memory:
{memory_content}

You can read and append to memory files using the memory tools.
Always read_memory at the start of a session to recall context."""
```

### 4. The agent decides what to remember

The agent is prompted to read memory at the start of each session and to write important information. This is not automatic -- the model decides what is worth persisting. Key moments where the agent should write to memory:

- When a user introduces themselves or states a preference
- When a decision is made about project architecture
- When a task is completed and should not be re-done next session
- When the agent learns something about the environment

### 5. Consolidation: token-budget driven memory compression

In long-running sessions, the conversation history grows past the context window. The **Consolidator** solves this by summarizing old messages via LLM and archiving them to `history.jsonl`:

```
   Messages list (growing)
         |
         v
+------------------+
| Token estimator  |  4 chars per token heuristic
+------------------+
         |
   Over budget?  ──No──→ Continue
         |
        Yes
         |
         v
+------------------+
| pick_boundary()  |  Find oldest user-turn boundary
+------------------+
         |
         v
+------------------+
| archive(chunk)   |  LLM summarization → history.jsonl
+------------------+
         |
         v
+------------------+
| Truncate messages|  Keep only the recent portion
+------------------+
```

```python
class Consolidator:
    def __init__(self, archive_dir, api_key=None):
        self.archive_dir = archive_dir
        self.history_file = archive_dir / "history.jsonl"
        self.client = Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))

    def maybe_consolidate(self, messages, budget):
        """If messages exceed budget, consolidate the oldest portion."""
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
            self.raw_archive(chunk)  # LLM failed, dump raw

        return remaining, summary

    def archive(self, messages):
        """Summarize messages via LLM and write to history.jsonl."""
        formatted = self._format_chunk(messages)
        summary = self._summarize(formatted)
        if summary:
            record = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": "consolidation",
                "summary": summary,
                "original_count": len(messages),
            }
            with open(self.history_file, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return summary

    def raw_archive(self, messages):
        """Fallback: dump messages directly without LLM summarization."""
        record = {"type": "raw_archive", ...}
        # ... writes raw text to history.jsonl
```

The `history.jsonl` file is an append-only log of consolidated chunks. Each record has a timestamp, type (consolidation or raw_archive), summary text, and original message count. Over time, this file becomes a browsable history of what was discussed and what was summarized.

Key design points:
- **Token budget** (e.g. 40K tokens) defines when to trigger consolidation
- **User-turn boundaries** ensure clean cut points
- **LLM summarization** preserves key facts while dropping implementation details
- **Raw fallback** prevents data loss if the LLM call fails
- **history.jsonl** is append-only so no existing data is ever modified

This pattern is inspired by the reference implementation's Consolidator, which uses the same token-budget driven approach with more sophisticated features like multi-round consolidation and session metadata tracking.

## What Changed From s04

| Component | Before (s03) | After (s04) |
|-----------|--------------|-------------|
| Persistence | No memory -- blank session each time | File-based memory loaded at startup and written during session |
| Tool set | `bash`, `read`, `write`, `load_skill` | Added `read_memory`, `write_memory` |
| System prompt | Skill summaries only | Skill summaries + loaded memory content |
| Consolidation | Not implemented | `Consolidator` with token-budget LLM summarization to `history.jsonl` |
| history.jsonl | Does not exist | Append-only archive of consolidated conversation chunks |
| Token estimation | None | `estimate_tokens()` / `estimate_messages_tokens()` 4-char heuristic |
| Cross-session state | None | Memory files persist across sessions |
| Startup behavior | Static setup | `MemoryManager.load_all()` reads prior memory files |
| Data storage | Skills directory only | `.memory/` directory with `MEMORY.md` + `history.jsonl` |

## Try It

```bash
python chapters/05_memory.py
```

Suggested prompts:

- "Remember that my name is Alice and I prefer verbose explanations."
- "What do you remember from our previous conversation?"
- "Append a note that we decided to use FastAPI for the backend."
- "Read the memory files and tell me what you know about me."

## Design Trade-offs

| Approach | Pros | Cons |
|----------|------|------|
| **Flat files** (this session) | Simple, transparent, no infra | No search, no ranking, grows unbounded |
| **Vector DB (RAG)** | Semantic search, ranking, scaling | Ops burden, embedding costs, latency |
| **Structured DB (SQLite)** | Queryable, relational, ACID | Schema migration overhead, rigid |
| **Agent-written summaries** | Compact, relevant, human-readable | Model decides quality; may miss details |

The flat-file approach is chosen because it is the simplest thing that works. When your use case outgrows it, you have a clear migration path: replace the `MemoryManager` with a vector-backed implementation behind the same tool interface.

## The Philosophy

Memory does not need to be magical. The model has a context window; everything that fits in the window is "remembered." Memory is just making sure the right things are in that window at session start. A file on disk, read at startup, satisfies this perfectly for a vast range of practical use cases.