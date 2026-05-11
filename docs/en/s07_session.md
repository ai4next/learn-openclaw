# s07: Session Management


> **"A session is just a JSONL file."**
> Harness layer: Persistence

## Problem

In s07, messages flow through the bus but nothing is saved. When the agent processes a turn, the messages are passed to the LLM as a list and then discarded. If the agent restarts, all conversation history is lost. If a user returns after an hour, the agent has no memory of who they are or what they were discussing.

Without persistence, the agent cannot:
- Remember context across restarts
- Maintain separate conversations for different users or channels
- Recover from crashes without losing history
- Support long-running conversations that span days or weeks

## Solution

Store each conversation as a **JSONL (JSON Lines) file** on disk -- one file per session, one JSON object per line. The first line is a metadata header; subsequent lines are individual messages. A `SessionManager` loads sessions on demand, caches them in memory, and writes back atomically.

```
Session file: sessions/telegram_123456.jsonl

Line 1:  {"_type": "metadata", "key": "telegram:123456",
           "created_at": "2026-05-11T10:00:00",
           "updated_at": "2026-05-11T12:30:00",
           "last_consolidated": 0}
Line 2:  {"role": "user",      "content": "Hello!",         "timestamp": "..."}
Line 3:  {"role": "assistant", "content": "Hi! How can I help?", "timestamp": "..."}
Line 4:  {"role": "user",      "content": "What's the weather?",
           "timestamp": "...", "media": ["/tmp/screenshot.png"]}
Line 5:  {"role": "tool",      "tool_call_id": "abc123",
           "content": "Sunny, 72F", "timestamp": "..."}


In memory (cached):

  SessionManager._cache = {
    "telegram:123456": Session(key="telegram:123456", messages=[...]),
    "discord:789":     Session(key="discord:789", messages=[...]),
    "cli:direct":      Session(key="cli:direct", messages=[...]),
  }
```

Each channel+chat combination has its own file. The `session_key` is typically `"channel:chat_id"`, so Telegram chat 123456 and Discord channel 789 never share history.

## How It Works

1. **Session** is an in-memory representation with add/get/clear methods.

```python
@dataclass
class Session:
    key: str  # e.g. "telegram:123456"
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs) -> None:
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages=120, *, max_tokens=0,
                    include_timestamps=False) -> list[dict]:
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]
        # Slice further by token budget if max_tokens > 0
        # Returns a clean list aligned to user turns
        return sliced
```

2. **SessionManager** loads from disk, caches in memory, and saves with atomic writes.

```python
class SessionManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(workspace / "sessions")
        self._cache: dict[str, Session] = {}

    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            return self._cache[key]
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        self._cache[key] = session
        return session

    def save(self, session: Session, *, fsync: bool = False) -> None:
        path = self._get_session_path(session.key)
        tmp_path = path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            # Write metadata header
            f.write(json.dumps({
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated,
            }, ensure_ascii=False) + "\n")
            # Write messages
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic on POSIX
        self._cache[session.key] = session
```

3. **Atomic write pattern** prevents file corruption. Write to a `.tmp` file, then `os.replace()` to atomically swap it into place. On graceful shutdown, `fsync` ensures durability even on FUSE/NFS mounts.

```python
def _atomic_write(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    # fsync parent directory for rename durability
    with suppress(PermissionError):
        fd = os.open(str(path.parent), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
```

4. **Session key derivation** ties each session to a specific channel and chat.

```python
# In InboundMessage:
@property
def session_key(self) -> str:
    return self.session_key_override or f"{self.channel}:{self.chat_id}"

# Session file: sessions/telegram_123456.jsonl
# Session file: sessions/discord_789.jsonl
# Session file: sessions/cli_direct.jsonl
```

5. **Corrupt file recovery** attempts to salvage messages from a broken JSONL.

```python
def _repair(self, key: str) -> Session | None:
    path = self._get_session_path(key)
    messages = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line.strip())
                if data.get("_type") == "metadata":
                    continue
                messages.append(data)
            except json.JSONDecodeError:
                skipped += 1
    if not messages:
        return None
    return Session(key=key, messages=messages)
```

The session file is append-friendly (one JSON object per line), human-readable, and trivially greppable. No database needed.

## What Changed From s07

| Component | Before (s06) | After (s07) |
|-----------|-------------|-------------|
| Conversation history | Lost after turn ends | Persisted to JSONL files |
| Session isolation | None -- all users share one message list | Each `channel:chat_id` gets its own session file |
| Restart behavior | All context lost | Full history restored from disk |
| File format | N/A | JSONL -- one JSON object per line |
| Write strategy | N/A | Atomic write via `.tmp` + `os.replace` |
| Crash recovery | N/A | Corrupt file repair salvages valid lines |
| Cache layer | N/A | In-memory `Session` cache with lazy loading |
| Session key | N/A | `channel:chat_id` (e.g. `telegram:123456`) |
| History query | Full list always | Slice by count or token budget |
| Data directory | N/A | `workspace/sessions/*.jsonl` |

## Try It

```bash
python chapters/08_session.py
```

Suggested prompts:
- `Say hello, then exit and restart. Is my previous message still there?`
- `Look at the session file in workspace/sessions/ -- what does the JSONL format look like?`
- `What happens if I corrupt the JSONL file by editing it manually? Can the session manager recover?`

---

**Design Note:** In the reference implementation, `SessionManager` handles the full lifecycle: `get_or_create` with cache, `save` with atomic fsync, `flush_all` on shutdown, `_repair` for corrupt files, `list_sessions` for the WebUI, and `enforce_file_cap` to bound message growth. The `Session.get_history()` method handles token-budgeted slicing, orphan tool-result pruning, image breadcrumb synthesis, and assistant replay-text sanitization.