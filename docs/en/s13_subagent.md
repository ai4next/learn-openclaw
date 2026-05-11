# s13: Subagent System


> **"A subagent is the same loop in a separate context."**
>
> Harness layer: Parallel Execution

## Problem

Many tasks are inherently parallel: "Research topic X and topic Y, then compare them." With a single agent loop, the model must do these sequentially, losing context and time between switches. What if the agent could **spawn child agents** to work in parallel?

The solution is a **subagent system**: the parent agent calls a `spawn` tool to create a subagent with its own task, collects results via a `collect` tool, and injects subagent outputs mid-turn through a pending queue.

## Solution

```
+-----------+     +-----------+     +-----------+
| Parent    | --> | Spawn     | --> | Subagent  |
| Agent     |     | Tool      |     | Runner    |
+-----------+     +-----------+     +-----------+
                        |                 |
                        v                 v
                 +-----------+     +-----------+
                 | Subagent  | --> | Pending   |
                 | Manager   | --> | Queue     |
                 +-----------+     +-----------+
                                         |
                                         v
                                   +-----------+
                                   | Mid-turn  |
                                   | Injection |
                                   +-----------+
```

The parent spawns a subagent and gets a `task_id`. The subagent runs in a background thread. The parent continues its own turn. When the subagent finishes, its result is queued for mid-turn injection — the parent sees it as a follow-up message.

## How It Works

### 1. Subagent

A subagent is the same agent loop running independently:

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

### 2. SubagentManager

Manages the lifecycle: spawn, track, collect:

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

### 3. SpawnTool and CollectTool

Two tools let the agent control subagents:

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

### 4. Parallel exploration workflow

The spawn/collect pattern enables powerful workflows:

1. **Fan-out**: spawn multiple subagents for different topics
2. **Collect**: gather all results
3. **Synthesize**: parent agent compares and summarizes

## What Changed From s13

| Component | Before (s12) | After (s13) |
|-----------|--------------|-------------|
| Execution | Single-threaded sequential | Background subagents in threads |
| Tool set | `bash`, `read` | Added `spawn`, `collect` |
| Parallelism | None | Fan-out subagents with task IDs |
| Result collection | Sequential only | Non-blocking collect by task ID |
| Injection | N/A | `PendingQueue` for mid-turn results |

## Try It

```bash
python chapters/14_subagent.py
```

Commands:

- `/spawn "Explain the concept of recursion in programming"` — subagent works in background
- `/spawn "List the key features of Python 3.13"` — second subagent in parallel
- `/collect` — collect all completed results
- `/status` — check pending count
- `/wait` — block until all complete

## Key Design Decisions

1. **Same loop, different context.** Subagents are not "lite" agents. They use the full agent loop. This keeps the architecture uniform.

2. **Thread-based, not process-based.** For CPU-bound LLM calls, threads are sufficient. Process isolation (for security) is covered in s13.

3. **Non-blocking by default.** The parent can continue working while subagents run. Collection is explicit — the parent decides when to check.

## Reference

This pattern follows the reference implementation's subagent architecture: a `SubagentManager` for lifecycle tracking and `SpawnTool`/`CollectTool` for agent-facing control. The pending queue for mid-turn injection mirrors the `_drain_pending` pattern that allows subagent results to be injected into the parent agent's ongoing turn.