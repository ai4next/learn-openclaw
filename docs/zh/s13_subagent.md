# s13: 子代理系统


> **"子代理就是在独立上下文中运行的同一个循环。"**
>
> 框架层：并行执行

## 问题

许多任务天生就适合并行处理："研究主题 X 和主题 Y，然后比较它们。"在单代理循环中，模型必须按顺序执行这些任务，在切换之间丢失上下文和时间。如果代理能够**派生子代理**来并行工作呢？

解决方案是**子代理系统**：父代理调用 `spawn` 工具创建带有自身任务的子代理，通过 `collect` 工具收集结果，并在同一轮次中通过待处理队列注入子代理的输出。

## 解决方案

```
+-----------+     +-----------+     +-----------+
| 父代理     | --> | Spawn     | --> | 子代理     |
|           |     | 工具      |     | 运行器     |
+-----------+     +-----------+     +-----------+
                        |                 |
                        v                 v
                 +-----------+     +-----------+
                 | 子代理     | --> | 待处理    |
                 | 管理器     | --> | 队列      |
                 +-----------+     +-----------+
                                         |
                                         v
                                   +-----------+
                                   | 同一轮次  |
                                   | 注入      |
                                   +-----------+
```

父代理派生子代理并获取一个 `task_id`。子代理在后台线程中运行。父代理继续执行自己的轮次。当子代理完成时，其结果被放入队列等待同一轮次注入——父代理会将其视为后续消息。

## 工作原理

### 1. 子代理

子代理是独立运行的同一个代理循环：

```python
class Subagent:
    def __init__(self, task_id, prompt, client, model):
        self.task_id = task_id
        self.prompt = prompt

    def run(self, timeout=120) -> SubagentResult:
        response = self.client.messages.create(
            model=self.model,
            system="You are a subagent. Complete the assigned task...",
            messages=[{"role": "user", "content": self.prompt}],
            max_tokens=4096,
        )
        content = "".join(b.text for b in response.content if b.type == "text")
        return SubagentResult(self.task_id, content.strip())
```

### 2. 子代理管理器

管理生命周期：生成、跟踪、收集：

```python
class SubagentManager:
    def spawn(self, prompt: str) -> str:
        task_id = uuid4().hex[:12]
        agent = Subagent(task_id, prompt, ...)
        thread = Thread(target=self._run, args=(agent,))
        thread.start()
        return task_id

    def collect(self, task_id: str) -> SubagentResult | None:
        return self._results.pop(task_id, None)
```

### 3. SpawnTool 和 CollectTool

两个工具让代理控制子代理：

```python
class SpawnTool:
    name = "spawn"
    def execute(self, prompt: str) -> str:
        task_id = mgr.spawn(prompt)
        return f"Subagent spawned. Task ID: {task_id}"

class CollectTool:
    name = "collect"
    def execute(self, task_id: str = "") -> str:
        if task_id:
            result = mgr.collect(task_id)
            return f"[{result.task_id}] {result.content}"
        results = mgr.collect_all()
        return "\n".join(f"[{r.task_id}] {r.content[:200]}" for r in results)
```

### 4. 并行探索工作流

spawn/collect 模式支持强大的工作流：

1. **扇出**：为不同主题派发多个子代理
2. **收集**：汇总所有结果
3. **综合**：父代理进行比较和总结

## 从 s13 变更的内容

| 组件 | 变更前 (s12) | 变更后 (s13) |
|-----------|--------------|-------------|
| 执行 | 单线程顺序执行 | 线程中的后台子代理 |
| 工具集 | `bash`, `read` | 新增 `spawn`, `collect` |
| 并行性 | 无 | 带任务 ID 的扇出子代理 |
| 结果收集 | 仅顺序收集 | 按任务 ID 非阻塞收集 |
| 注入 | 不适用 | 用于同一轮次结果的 `PendingQueue` |

## 尝试运行

```bash
python chapters/14_subagent.py
```

命令：

- `/spawn "Explain the concept of recursion in programming"` — 子代理在后台工作
- `/spawn "List the key features of Python 3.13"` — 第二个子代理并行运行
- `/collect` — 收集所有已完成的结果
- `/status` — 检查待处理数量
- `/wait` — 阻塞直到全部完成

## 关键设计决策

1. **相同的循环，不同的上下文。** 子代理不是"轻量级"代理。它们使用完整的代理循环。这保持了架构的一致性。

2. **基于线程，而非基于进程。** 对于 CPU 密集型的 LLM 调用，线程已经足够。进程隔离（用于安全性）在 s13 中涉及。

3. **默认非阻塞。** 父代理可以在子代理运行时继续工作。收集是显式的——父代理决定何时检查。

## 参考

该模式遵循参考实现的子代理架构：`SubagentManager` 管理生命周期，`SpawnTool`/`CollectTool` 提供代理端控制接口。用于同一轮次注入的待处理队列借鉴了 `_drain_pending` 模式，允许将子代理结果注入到父代理正在进行的轮次中。