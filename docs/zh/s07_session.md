# s07: 会话管理


> **"会话就是一个 JSONL 文件。"**
> Harness 层：持久化

## 问题

在 s06 中，消息通过总线流动，但没有任何内容被保存。当 agent 处理完一轮对话时，消息作为列表传递给 LLM 然后就被丢弃。如果 agent 重启，所有对话历史都会丢失。如果用户在一小时后返回，agent 对他们是谁以及他们在讨论什么一无所知。

没有持久化，agent 无法：
- 在重启后记住上下文
- 为不同用户或频道维护独立的对话
- 在崩溃后恢复而不丢失历史记录
- 支持跨越数天或数周的长时间对话

## 解决方案

将每个对话作为 **JSONL（JSON Lines）文件** 存储在磁盘上 —— 每个会话一个文件，每行一个 JSON 对象。第一行是元数据头部；后续行是独立的消息。`SessionManager` 按需加载会话，在内存中缓存它们，并通过原子写入持久化。

```
会话文件：sessions/telegram_123456.jsonl

第 1 行： {"_type": "metadata", "key": "telegram:123456",
           "created_at": "2026-05-11T10:00:00",
           "updated_at": "2026-05-11T12:30:00",
           "last_consolidated": 0}
第 2 行： {"role": "user",      "content": "Hello!",         "timestamp": "..."}
第 3 行： {"role": "assistant", "content": "Hi! How can I help?", "timestamp": "..."}
第 4 行： {"role": "user",      "content": "What's the weather?",
           "timestamp": "...", "media": ["/tmp/screenshot.png"]}
第 5 行： {"role": "tool",      "tool_call_id": "abc123",
           "content": "Sunny, 72F", "timestamp": "..."}


内存中（缓存）：

  SessionManager._cache = {
    "telegram:123456": Session(key="telegram:123456", messages=[...]),
    "discord:789":     Session(key="discord:789", messages=[...]),
    "cli:direct":      Session(key="cli:direct", messages=[...]),
  }
```

每个频道+聊天的组合都有自己的文件。`session_key` 通常是 `"channel:chat_id"`，因此 Telegram 聊天 123456 和 Discord 频道 789 永远不会共享历史记录。

## 工作原理

1. **Session** 是一个内存中的表示，提供 add/get/clear 方法。

```python
@dataclass
class Session:
    key: str  # 例如 "telegram:123456"
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # 已合并到文件的消息数

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
        # 如果 max_tokens > 0，则进一步按 token 预算切片
        # 返回按用户轮次对齐的干净列表
        return sliced
```

2. **SessionManager** 从磁盘加载、在内存中缓存，并通过原子写入保存。

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
            # 写入元数据头部
            f.write(json.dumps({
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated,
            }, ensure_ascii=False) + "\n")
            # 写入消息
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, path)  # 在 POSIX 上是原子操作
        self._cache[session.key] = session
```

3. **原子写入模式** 防止文件损坏。先写入一个 `.tmp` 文件，然后通过 `os.replace()` 原子性地将其替换到目标位置。在正常关闭时，`fsync` 确保即使在 FUSE/NFS 挂载上也具备持久性。

```python
def _atomic_write(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    # 对父目录执行 fsync 以确保重命名操作的持久性
    with suppress(PermissionError):
        fd = os.open(str(path.parent), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
```

4. **会话键的推导** 将会话绑定到特定的频道和聊天。

```python
# 在 InboundMessage 中：
@property
def session_key(self) -> str:
    return self.session_key_override or f"{self.channel}:{self.chat_id}"

# 会话文件：sessions/telegram_123456.jsonl
# 会话文件：sessions/discord_789.jsonl
# 会话文件：sessions/cli_direct.jsonl
```

5. **损坏文件恢复** 尝试从损坏的 JSONL 中抢救消息。

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

会话文件适合追加写入（每行一个 JSON 对象）、人类可读，并且可以轻松地用 grep 搜索。无需数据库。

## s07 的变化

| 组件 | 之前 (s06) | 之后 (s07) |
|-----------|-------------|-------------|
| 对话历史 | 轮次结束后丢失 | 持久化到 JSONL 文件 |
| 会话隔离 | 无 —— 所有用户共享一个消息列表 | 每个 `channel:chat_id` 拥有自己的会话文件 |
| 重启行为 | 所有上下文丢失 | 从磁盘恢复完整历史 |
| 文件格式 | 无 | JSONL —— 每行一个 JSON 对象 |
| 写入策略 | 无 | 通过 `.tmp` + `os.replace` 原子写入 |
| 崩溃恢复 | 无 | 损坏文件修复，抢救有效行 |
| 缓存层 | 无 | 内存中 `Session` 缓存，支持延迟加载 |
| 会话键 | 无 | `channel:chat_id`（例如 `telegram:123456`） |
| 历史查询 | 总是返回完整列表 | 按数量或 token 预算切片 |
| 数据目录 | 无 | `workspace/sessions/*.jsonl` |

## 试试看

```bash
python chapters/08_session.py
```

建议的提示词：
- `打个招呼，然后退出并重启。我之前的消息还在吗？`
- `查看 workspace/sessions/ 中的会话文件 —— JSONL 格式是什么样的？`
- `如果我手动编辑损坏了 JSONL 文件，会发生什么？SessionManager 能恢复吗？`

---

**设计说明：** 在参考实现中，`SessionManager` 处理完整的生命周期：带缓存的 `get_or_create`、带原子 fsync 的 `save`、关闭时的 `flush_all`、损坏文件的 `_repair`、WebUI 的 `list_sessions`，以及限制消息增长的 `enforce_file_cap`。`Session.get_history()` 方法负责按 token 预算切片、清理孤立工具结果、合成图像面包屑以及清理辅助回复文本。