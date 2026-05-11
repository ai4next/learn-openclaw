# s04: 记忆系统


> **"记忆只是一个文件。不需要向量数据库。"**
>
> 基础设施层：持久化

## 问题

每个会话都从空白对话开始。代理没有先前交互的记忆——用户提到过的名字、做出的决定、表达过的偏好、完成的工作。对于一个工具构建助手或长期运行的项目伴侣，这种健忘意味着用户必须在每个会话中重复上下文。

企业级解决方案会使用向量数据库、嵌入管道和检索增强生成（RAG）。但对于许多用例来说，这些都是杀鸡用牛刀。解决方案是**基于文件的记忆**：持久化、人类可读的 markdown 文件，代理在启动时读取并在会话期间更新。记忆只是一个文件。代理写入它想要记住的内容；它读取已经被记住的内容。

## 解决方案

```
   会话开始               会话期间                  下次会话
       |                       |                          |
       v                       v                          v
+--------------+       +----------------+          +--------------+
| 从磁盘读取   |       | 代理写入更新   |          | 从磁盘读取   |
| 记忆文件     |       | 到记忆文件     |          | 记忆文件     |
+--------------+       +----------------+          +--------------+
       |                       |                          |
       v                       v                          v
+--------------+       +----------------+          +--------------+
| 注入到       |       | 代理读取       |          | 注入到       |
| 系统提示     |       | 记忆以获取     |          | 系统提示     |
|              |       | 上下文         |          |              |
+--------------+       +----------------+          +--------------+
```

记忆以纯 markdown 文件形式存储在 `memory/` 目录中。每个文件代表一个主题领域（用户偏好、项目笔记、决策）。代理拥有 `read_memory` 和 `append_memory` 工具来访问和更新这些文件。在会话开始时，代理读取所有记忆文件并将其包含在上下文中。

## 工作原理

### 1. 定义记忆目录结构

记忆文件存放在工作目录下的 `memory/` 目录中：

```
memory/
  user-preferences.md    # 用户的姓名、偏好风格、设置
  project-status.md      # 当前项目状态、已完成的任务
  decisions.md           # 架构决策及其理由
  learnings.md           # 代理发现的内容
```

每个文件都是纯 Markdown 格式。代理写入这些文件；基础设施在启动时读取它们。

### 2. 添加记忆工具

两个新工具扩展了工具系统：

- **`read_memory_dir`** -- 列出可用的记忆文件并返回其内容
- **`append_memory`** -- 向记忆文件追加新条目（或创建它）

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

### 3. 在启动时将记忆注入系统提示

当代理启动时，基础设施读取所有记忆文件并将其包含在系统提示或作为初始工具结果中：

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

代理循环在用户第一轮对话之前加载记忆：

```python
memory_manager = MemoryManager(WORKDIR / "memory")
memory_content = memory_manager.load_all()

SYSTEM_PROMPT = f"""You are an OpenClaw agent with persistent memory.

Previous session memory:
{memory_content}

You can read and append to memory files using the memory tools.
Always read_memory at the start of a session to recall context."""
```

### 4. 代理决定记住什么

代理被提示在每个会话开始时读取记忆并写入重要信息。这不是自动的——模型决定什么值得持久化。代理应该写入记忆的关键时刻：

- 当用户介绍自己或表达偏好时
- 当对项目架构做出决定时
- 当任务完成且不应在下个会话中重复执行时
- 当代理了解到有关环境的某些信息时

### 5. 整合：基于 Token 预算的记忆压缩

在长时间运行的会话中，对话历史会增长到超过上下文窗口。**整合器（Consolidator）** 通过 LLM 总结旧消息并将其归档到 `history.jsonl` 来解决这个问题：

```
   消息列表（增长中）
         |
         v
+------------------+
| Token 估算器     |  4 字符每 token 启发式
+------------------+
         |
   超出预算？──否──→ 继续
         |
        是
         |
         v
+------------------+
| pick_boundary()  |  找到最旧的用户轮次边界
+------------------+
         |
         v
+------------------+
| archive(chunk)   |  LLM 总结 → history.jsonl
+------------------+
         |
         v
+------------------+
| 截断消息         |  仅保留最近的部分
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

`history.jsonl` 文件是一个仅追加的整合块日志。每条记录包含时间戳、类型（consolidation 或 raw_archive）、摘要文本和原始消息数量。随着时间的推移，这个文件变成了可浏览的历史记录，记录了讨论过和总结过的内容。

关键设计要点：
- **Token 预算**（例如 40K token）定义了何时触发整合
- **用户轮次边界**确保干净的切割点
- **LLM 总结**保留关键事实，同时舍弃实现细节
- **原始回退**防止 LLM 调用失败时数据丢失
- **history.jsonl** 是仅追加的，因此不会修改任何现有数据

这种模式受到参考实现的 Consolidator 的启发，它使用了相同的基于 token 预算的方法，并具有更高级的功能，如多轮整合和会话元数据跟踪。

## s04 的变化

| 组件 | 之前（s03） | 之后（s04） |
|-----------|--------------|-------------|
| 持久化 | 无记忆——每次会话都是空白 | 基于文件的记忆，启动时加载，会话期间写入 |
| 工具集 | `bash`、`read`、`write`、`load_skill` | 新增 `read_memory`、`write_memory` |
| 系统提示 | 仅技能摘要 | 技能摘要 + 已加载的记忆内容 |
| 整合 | 未实现 | `Consolidator`，基于 token 预算的 LLM 总结到 `history.jsonl` |
| history.jsonl | 不存在 | 仅追加的整合会话块归档 |
| Token 估算 | 无 | `estimate_tokens()` / `estimate_messages_tokens()` 4 字符启发式 |
| 跨会话状态 | 无 | 记忆文件跨会话持久化 |
| 启动行为 | 静态设置 | `MemoryManager.load_all()` 读取先前的记忆文件 |
| 数据存储 | 仅技能目录 | `.memory/` 目录，包含 `MEMORY.md` + `history.jsonl` |

## 尝试运行

```bash
python chapters/05_memory.py
```

建议的提示词：

- "记住我的名字是 Alice，我更喜欢详细的解释。"
- "你记得我们上次对话的什么内容？"
- "追加一条笔记：我们决定使用 FastAPI 作为后端。"
- "读取记忆文件并告诉我你了解我的哪些信息。"

## 设计权衡

| 方法 | 优点 | 缺点 |
|----------|------|------|
| **平面文件**（本方案） | 简单、透明、无需基础设施 | 无搜索、无排序、无限增长 |
| **向量数据库（RAG）** | 语义搜索、排序、可扩展 | 运维负担、嵌入成本、延迟 |
| **结构化数据库（SQLite）** | 可查询、关系型、ACID | 模式迁移开销、僵化 |
| **代理编写的摘要** | 紧凑、相关、人类可读 | 模型决定质量；可能遗漏细节 |

选择平面文件方法是因为它是最简单的可行方案。当你的用例超出其能力时，你有一条清晰的迁移路径：用向量后端的实现替换 `MemoryManager`，同时保持相同的工具接口。

## 哲学

记忆不需要是神奇的。模型有一个上下文窗口；所有适合该窗口的内容都被"记住"。记忆只是确保正确的内容在会话开始时位于该窗口中。存储在磁盘上的文件，在启动时读取，对于绝大多数实际用例来说完美地满足了这一点。