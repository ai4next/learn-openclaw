# s05: 上下文管理


> **"上下文窗口是有限的。框架负责管理什么能放进去。"**
>
> 框架层：上下文治理

## 问题

Agent 每交换一条消息——用户输入、助手响应、工具结果、记忆内容、技能主体——都会累积在 `messages` 列表中。LLM 上下文窗口是有限的（通常为 8K 到 200K tokens）。长时间运行的会话最终会超过限制，导致错误、性能下降或静默截断上下文。

简单地裁剪最早的消息是幼稚的做法：一条旧消息可能包含关键上下文（用户的名字、某个关键决策、正在编辑的文件路径）。Agent 需要**智能上下文管理**：token 预算、选择性剪枝、自动压缩，以及对哪些内容可以安全移除的结构化认知。

## 解决方案

```
                     +-----------+
                     | Token     |
                     | 计数器    |
                     +-----------+
                           |
         +-----------------+-----------------+
         |                                    |
         v                                    v
+--------------------+             +--------------------+
| 预算               |             | 压缩               |
| 强制               |             | 引擎               |
|（每次调用前）      |             |（超出预算时）      |
+--------------------+             +--------------------+
         |                                    |
         v                                    v
+-----------------------------------+  +--------------------+
| 1. 统计当前 token 数             |  | 1. 总结最早的非    |
| 2. 预估新一轮所需空间            |  |    必要消息        |
| 3. 若超预算，触发                |  | 2. 移除 tool_use  |
|    压缩                          |  |    链              |
| 4. 执行 LLM 调用                 |  | 3. 精简系统        |
+-----------------------------------+  |    提示            |
                                       +--------------------+
```

上下文管理器位于 Agent 循环和 LLM 调用之间。每次请求前，它会统计 token 数、估算预算，并在需要时触发压缩。压缩产生更短但语义等价的对话历史。

## 工作原理

### 1. Token 计数

`TokenCounter` 封装了一个 tiktoken 编码器（或 Anthropic tokenizer），用于统计消息中的 token 数而无需发送到 API。这一点至关重要——你需要在调用之前就知道预算情况。

```python
class TokenCounter:
    def __init__(self, model: str = "claude"):
        # Use tiktoken or Anthropic's tokenizer
        import tiktoken
        self.encoder = tiktoken.get_encoding("cl100k_base")

    def count_messages(self, messages: list) -> int:
        """Count total tokens in the messages list."""
        total = 0
        for msg in messages:
            total += self._count_message(msg)
        return total

    def _count_message(self, msg: dict) -> int:
        tokens = 4  # overhead per message
        content = msg.get("content", "")
        if isinstance(content, str):
            tokens += len(self.encoder.encode(content))
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content", "")
                    tokens += len(self.encoder.encode(str(text)))
        return tokens

    def estimate_fit(self, messages: list, max_tokens: int) -> tuple[bool, int]:
        """Return (fits_within_budget, current_token_count)."""
        current = self.count_messages(messages)
        return current < max_tokens, current
```

### 2. 定义预算

上下文预算定义了 Agent 可以使用多少 token。其中一部分保留给即将生成的响应：

```python
@dataclass
class ContextBudget:
    max_total: int = 100_000    # Hard ceiling for total messages
    max_response: int = 4_096   # Reserve for the next response
    compaction_threshold: float = 0.8  # Compaction triggers at 80% of max_total
    system_prompt_budget: int = 20_000 # Max tokens for system prompt

    @property
    def message_budget(self) -> int:
        """Available budget for messages (excluding response reservation)."""
        return self.max_total - self.max_response

    def is_over_threshold(self, current_tokens: int) -> bool:
        threshold = int(self.max_total * self.compaction_threshold)
        return current_tokens >= threshold
```

### 3. 剪枝策略：优先移除什么

并非所有消息都具有同等价值。`CompactionEngine` 应用分层剪枝策略：

| 优先级 | 内容 | 原因 |
|--------|------|------|
| **优先移除** | 已解决任务的 `tool_use` / `tool_result` 链 | 这些已经完成；只有最终结果重要 |
| **其次移除** | 用户已看过的旧 assistant 响应 | 用户已经读过；只需要要点 |
| **总结** | 较长的对话段落 | 用 3 句话摘要替代 10 轮对话 |
| **精简** | 系统提示段落 | 移除重复指令，仅内联活跃的技能 |
| **始终保留** | 用户偏好、决策、关键上下文 | 这些定义了 Agent 对当前任务的理解 |

```python
class CompactionEngine:
    def __init__(self, token_counter: TokenCounter, budget: ContextBudget):
        self.counter = token_counter
        self.budget = budget

    def compact(self, messages: list) -> list:
        """Compact messages to fit within budget."""
        current_tokens = self.counter.count_messages(messages)
        if not self.budget.is_over_threshold(current_tokens):
            return messages  # No compaction needed

        # Phase 1: Remove resolved tool chains
        messages = self._prune_resolved_tools(messages)

        # Phase 2: Condense old conversation turns
        messages = self._condense_old_turns(messages)

        # Phase 3: If still over, summarize oldest segments
        messages = self._summarize_oldest(messages)

        return messages

    def _prune_resolved_tools(self, messages: list) -> list:
        """Remove tool_use/tool_result pairs that are no longer needed."""
        # Walk messages from oldest to newest. Keep the last N tool chains,
        # remove older ones whose results have been consumed.
        pruned = []
        tool_depth = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                has_tool_use = any(
                    b.get("type") == "tool_use" for b in content
                )
                has_tool_result = any(
                    b.get("type") == "tool_result" for b in content
                )
                if has_tool_result and tool_depth > 3:
                    # Replace this tool result with a summary marker
                    pruned.append({
                        "role": "user",
                        "content": "[Tool results from previous steps omitted for space]",
                    })
                    continue
                if has_tool_use:
                    tool_depth += 1
            pruned.append(msg)
        return pruned

    def _condense_old_turns(self, messages: list) -> list:
        """Replace old user/assistant pairs with condensed versions."""
        # Keep the last 20% of turns intact (most recent context).
        # Condense the first 80% into a single summary message.
        if len(messages) < 10:
            return messages

        cutoff = max(len(messages) // 5, 2)  # Keep at least 2 recent turns
        recent = messages[-cutoff:]
        old = messages[:-cutoff]

        condensed = [{
            "role": "user",
            "content": (
                "[The following is a condensed summary of earlier conversation. "
                f"The original had {len(old)} messages.]\n\n"
                "Earlier conversation covered user requests, tool calls, "
                "and responses that have been compacted to save context."
            ),
        }]
        return condensed + recent
```

### 4. 与 Agent 循环的集成

Agent 循环在每次 LLM 请求前调用 `ContextManager`：

```python
class ContextManager:
    def __init__(self, budget: ContextBudget = None):
        self.budget = budget or ContextBudget()
        self.counter = TokenCounter()
        self.compactor = CompactionEngine(self.counter, self.budget)

    def prepare_messages(self, messages: list) -> list:
        """Prepare messages for an LLM call: compact if needed."""
        messages = self.compactor.compact(messages)
        _, token_count = self.counter.estimate_fit(messages, self.budget.max_total)
        return messages

    def prepare_system_prompt(self, prompt: str) -> str:
        """Truncate or condense system prompt to fit budget."""
        tokens = self.counter.encoder.encode(prompt)
        if len(tokens) <= self.budget.system_prompt_budget:
            return prompt
        # Truncate to budget, keeping the beginning and end
        budget = self.budget.system_prompt_budget
        beginning = tokens[:budget // 2]
        ending = tokens[-(budget // 2):]
        truncated = self.counter.encoder.decode(beginning + ending)
        return truncated + "\n\n[System prompt truncated for length.]"


# Integration into the agent loop:

context_manager = ContextManager()

def agent_loop(messages):
    while True:
        messages = context_manager.prepare_messages(messages)

        content_blocks, stop_reason = provider.create(
            model=MODEL,
            system=context_manager.prepare_system_prompt(SYSTEM_PROMPT),
            messages=messages,
            max_tokens=4096,
            tools=registry.list_schemas(),
        )

        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason == "end_turn":
            break
        if stop_reason == "tool_use":
            for block in content_blocks:
                if block.type == "tool_use":
                    result = registry.execute(block.name, **block.input)
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }],
                    })

    return content_blocks
```

### 5. 系统提示精简

系统提示本身可能会变得很大（技能主体、记忆内容、工具描述）。上下文管理器会单独对其进行精简：

- 静态部分（身份、规则）保持原样。
- 动态部分（技能主体、记忆）被截断或用摘要替换。
- 工具 schema 保持完整（它们体积小且至关重要）。

## 从 s05 以来的变化

| 组件 | 之前 (s04) | 之后 (s05) |
|------|------------|------------|
| 上下文感知 | 无——消息无限制增长 | `TokenCounter` 在每次调用前衡量 token 使用量 |
| 预算强制 | 无限制 | `ContextBudget` 包含上限、响应预留、压缩阈值 |
| 压缩策略 | Consolidator 将旧轮次归档到 `history.jsonl` | `CompactionEngine` 采用分层剪枝：工具链、旧轮次、摘要 |
| AutoCompact | 无 | 基于 TTL 的会话过期检测，自动压缩 |
| Token 计数 | `estimate_tokens()` 4 字符启发式 | `TokenCounter.count_messages()` 支持 content_block 列表 |
| 系统提示管理 | 静态或手动更新 | `prepare_system_prompt()` 截断到预算 |
| 消息准备 | 原始消息传递给 API | `prepare_messages()` 在每次 LLM 调用前压缩 |
| 长会话支持 | 质量下降或崩溃 | 优雅压缩 + AutoCompact 实现扩展会话 |

## 试试看

```bash
python chapters/06_context.py
```

建议的提示语：

- "给我讲一个长故事，一次讲一段。每次我都说'继续'。"（模拟长会话，然后观察压缩）
- "你还记得我们对话开始时我说过哪些消息吗？"
- "创建 10 个随机内容的文件，然后把它们全部读给我听。"（生成大量 tool_use/tool_result 对以触发压缩）

## 压缩的权衡

| 策略 | 信息损失 | Token 节省 | 复杂度 |
|------|----------|------------|--------|
| 移除旧工具链 | 低（结果已被消费） | 高 | 低 |
| 截断旧消息 | 中（丢失细节） | 高 | 低 |
| 摘要段落 | 中低（LLM 保留要点） | 高 | 高 |
| 截断系统提示 | 中（丢失边缘细节） | 中 | 低 |

压缩引擎按照信息损失递增的顺序应用策略。关键原则是：**保留对话的语义核心**，同时丢弃冗余或已被消费的细节。压缩采取保守策略——宁可让一条长消息通过，也不可意外移除关键上下文。

### 6. AutoCompact——基于 TTL 的会话过期

除了每轮压缩之外，生产环境中的 Agent 还需要 **AutoCompact**：自动检测空闲会话并在后台进行清理。当会话在超过其 TTL（生存时间）后处于非活动状态时，旧消息会在下一轮对话前被压缩：

```python
class AutoCompact:
    def __init__(self, ttl_minutes: int = 30):
        self.ttl_minutes = ttl_minutes
        self._last_active: float = 0.0

    def check_expired(self, session_messages) -> bool:
        elapsed = time.time() - self._last_active
        return elapsed > self.ttl_minutes * 60

    def auto_compact_if_expired(self, messages, context_mgr):
        if not self.check_expired(messages):
            return messages, False
        messages = context_mgr.compact_messages(messages)
        messages = context_mgr.prune_history(messages, max_tokens=60000)
        self.mark_active()
        return messages, True
```

集成到 REPL 循环中，在对话轮次之间执行：

```python
while True:
    messages, was_compacted = auto_compact.auto_compact_if_expired(
        messages, ctx_mgr
    )
    if was_compacted:
        print("[AutoCompact] Session was idle — compacted automatically.")

    user_input = input(">> ")
    auto_compact.mark_active()
    messages.append({"role": "user", "content": user_input})
    # ...continue with LLM call...
```

此模式用于生产级代理框架中，其中 `AutoCompact` 在 Agent 循环的消息之间运行，无需用户干预即可保持长会话的整洁。