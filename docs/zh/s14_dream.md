# s14: 梦境记忆处理器


> **"梦是一个管理者，负责管理智能体的记忆。"**
>
> 编排层：元认知

## 问题

记忆文件会变得陈旧。事实会改变，偏好会演变，旧的项目状态会变得无关紧要。借助 `read_memory` 和 `write_memory` 工具（s05），智能体可以管理自己的记忆——但仅限于主动思考时。那些它没有注意到的记忆，或者它忘记记录的模式呢？

**梦境**处理器是一个后台智能体，定期回顾对话历史并自主更新记忆文件。它按 cron 计划（s11）运行，分析新的历史条目，并对 `MEMORY.md`、`SOUL.md` 和 `USER.md` 进行有针对性的编辑。

## 解决方案

```
+-----------+
| history.  |  仅追加的对话块日志
| jsonl     |
+-----------+
     |
+-----------+
| 阶段 1    |  LLM 分析历史：提取事实、
| 分析      |  检测模式、标记陈旧内容
+-----------+
     |
+-----------+
| 阶段 2    |  AgentRunner 使用 read_file / edit_file
| 编辑      |  编辑 MEMORY.md、SOUL.md、USER.md
+-----------+
```

两阶段设计将分析与行动分离。阶段 1 是普通的 LLM 调用，没有工具——它只进行阅读和思考。阶段 2 是完整的 AgentRunner，拥有文件系统工具——它读取当前文件，与分析结果进行比较，并进行有针对性的编辑。

## 工作原理

### 1. history.jsonl —— 原始素材

Dream 处理来自仅追加的 JSONL 日志的条目。每个条目包含一个游标、时间戳和内容：

```json
{"cursor": 1, "timestamp": "2026-05-11 10:30", "content": "User said they prefer Rust for systems programming"}
```

### 2. 阶段 1 —— 分析

LLM 读取自上次处理的游标以来的新条目，并生成分析结果：

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

### 3. 阶段 2 —— 编辑

分析结果驱动文件编辑。Dream 的阶段 2 拥有 `read_file` 和 `edit_file` 工具，并使用 AgentRunner 进行有针对性的更新：

```python
def _phase2_edit(self, analysis):
    current = self.store.read_memory()
    updated = current + "\n\n## Dream Update\n" + analysis[:2000]
    self.store.write_memory(updated)
```

### 4. 游标追踪

Dream 维护一个游标，以便只处理新条目：

```python
last_cursor = self.history.get_last_cursor()
entries = self.history.read_since(last_cursor)
# ... process ...
self.history.set_last_cursor(entries[-1]["cursor"])
```

## 从 s14 开始的变化

| 组件 | 之前（s13） | 之后（s14） |
|-----------|--------------|-------------|
| 记忆管理 | 通过工具手动管理 | 自主后台处理 |
| 历史格式 | 仅在内存中 | 仅追加的 `history.jsonl` 带游标 |
| 处理触发 | 用户命令 | Cron 计划调度（或手动 `/run`） |
| 分析深度 | 无 | 两阶段：分析后编辑 |
| 游标追踪 | 无 | `.dream_cursor` 文件用于增量处理 |

## 尝试运行

```bash
python chapters/15_dream.py
```

命令：

- `/add "User mentioned they prefer dark mode for the UI"` —— 模拟一条对话条目
- `/add "User decided to use PostgreSQL over MongoDB"` —— 另一条条目
- `/run` —— 运行 Dream 处理条目
- `/memory` —— 查看更新后的 MEMORY.md
- `/cursor` —— 检查最后处理的游标

## 关键设计决策

1. **两阶段分离。** 阶段 1 没有工具——只进行分析。阶段 2 拥有工具，但受阶段 1 的分析结果指导。这防止了智能体做出过早的编辑。

2. **仅追加历史。** JSONL 日志永远不会被修改——只追加内容。这使得它对并发写入安全且防崩溃。

3. **基于游标的增量处理。** Dream 只查看新条目。这使每次运行保持快速且幂等。
