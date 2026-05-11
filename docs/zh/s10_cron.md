# s10: 定时与调度


> **"一个只会响应的智能体，只能算半个智能体。"**
> Harness 层：自主运行（Autonomous Operation）

## 问题

在 s09 中，智能体是纯粹反应式的。它等待消息、处理消息、回复消息，然后再次等待。它永远无法主动发起行动。没有办法说"每天早上 8 点检查天气"或者"给我发送每周总结"或者"在 2 小时后提醒我 X"。

一个反应式智能体随时待命，但从不会主动出击。现实世界中的智能体需要能够：
- 执行周期性维护（记忆整合、心跳检测）
- 发送定时通知（早晨简报、截止日期提醒）
- 执行一次性延迟任务（"30 分钟后提醒我"）
- 即使没有人与之交谈也能自主运行

## 解决方案

添加一个 **CronService**，用于管理一个持久的定时任务列表。每个任务都有一个调度表达式（cron 表达式、固定间隔或一次性时间戳）和一个载荷（待处理的消息）。该服务运行一个定时器循环，在任务到期时唤醒、执行任务，然后安排下一次唤醒。

```
CronService 生命周期：

  start()
    -> _load_store()            # 从 jobs.json 加载任务
    -> _recompute_next_runs()   # 计算下一次执行的时间戳
    -> _arm_timer()             # 安排 asyncio.sleep

  _on_timer()
    -> _load_store()            # 如果文件已更改则热重载
    -> 查找到期任务 (due_jobs)
    -> 遍历每个任务: _execute_job()
    -> _save_store()            # 持久化运行历史
    -> _arm_timer()             # 安排下一次唤醒

定时器管理：

                        now                          next job
  time:  ----|-----------|-----------------------------|----------->
              \           \                             \
          _arm_timer()   asyncio.sleep(delay)         _on_timer()
                                                         \
                                                   _execute_job()
                                                         \
                                                   _arm_timer()

调度类型：
  "every":  每 N 毫秒              [---间隔---]
  "cron":   cron 表达式（如 "0 9 * * *"）[每天上午 9 点]
  "at":     在指定时间戳执行一次      [...X]
```

任务通过原子写入持久化到 `jobs.json` 文件中（与会话文件相同的模式）。该服务能在重启后存活——错过的任务将在下一个定时器周期执行。

## 工作原理

1. **CronSchedule 和 CronJob 数据类**定义了运行什么以及何时运行。

```python
@dataclass
class CronSchedule:
    kind: Literal["at", "every", "cron"]
    at_ms: int | None = None       # 用于 "at"：绝对时间戳（毫秒）
    every_ms: int | None = None    # 用于 "every"：间隔（毫秒）
    expr: str | None = None        # 用于 "cron"："0 9 * * *"
    tz: str | None = None          # cron 表达式的时区

@dataclass
class CronPayload:
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    deliver: bool = False          # 将响应发送到频道
    channel: str | None = None     # 例如 "telegram"
    to: str | None = None          # 例如 chat_id
    channel_meta: dict = field(default_factory=dict)
    session_key: str | None = None

@dataclass
class CronJob:
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False
```

2. **下一次运行的计算**将调度表达式转换为绝对时间戳。

```python
def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    if schedule.kind == "at":
        return (schedule.at_ms
                if schedule.at_ms and schedule.at_ms > now_ms
                else None)

    if schedule.kind == "every":
        return (now_ms + schedule.every_ms
                if schedule.every_ms and schedule.every_ms > 0
                else None)

    if schedule.kind == "cron" and schedule.expr:
        from zoneinfo import ZoneInfo
        from croniter import croniter
        tz = ZoneInfo(schedule.tz) if schedule.tz else ...
        base_dt = datetime.fromtimestamp(now_ms / 1000, tz=tz)
        cron = croniter(schedule.expr, base_dt)
        next_dt = cron.get_next(datetime)
        return int(next_dt.timestamp() * 1000)

    return None
```

3. **定时器循环**在最早到期时间唤醒、执行任务，然后重新布防。

```python
class CronService:
    def __init__(self, store_path: Path, on_job=None, max_sleep_ms=300_000):
        self.store_path = store_path
        self.on_job = on_job  # 回调函数：async def on_job(job) -> str|None
        self._timer_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()

    def _arm_timer(self):
        if self._timer_task:
            self._timer_task.cancel()
        next_wake = self._get_next_wake_ms()
        delay_ms = min(self.max_sleep_ms,
                       max(0, (next_wake or self.max_sleep_ms) - _now_ms()))

        async def tick():
            await asyncio.sleep(delay_ms / 1000)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self):
        self._load_store()  # 热重载
        now = _now_ms()
        due_jobs = [j for j in self._store.jobs
                    if j.enabled and j.state.next_run_at_ms
                    and now >= j.state.next_run_at_ms]
        for job in due_jobs:
            await self._execute_job(job)
        self._save_store()
        self._arm_timer()
```

4. **任务执行**调用 `on_job` 回调（该回调将任务路由到智能体循环）。

```python
async def _execute_job(self, job: CronJob) -> None:
    start_ms = _now_ms()
    try:
        if self.on_job:
            await self.on_job(job)
        job.state.last_status = "ok"
    except Exception as e:
        job.state.last_status = "error"
        job.state.last_error = str(e)

    job.state.last_run_at_ms = start_ms
    job.state.run_history.append(CronRunRecord(
        run_at_ms=start_ms,
        status=job.state.last_status,
        duration_ms=_now_ms() - start_ms,
    ))

    # 一次性任务：禁用或删除
    if job.schedule.kind == "at":
        if job.delete_after_run:
            self._store.jobs.remove(job)
        else:
            job.enabled = False
            job.state.next_run_at_ms = None
    else:
        job.state.next_run_at_ms = _compute_next_run(
            job.schedule, _now_ms()
        )
```

5. **智能体工具集成**让模型能够在运行时创建定时任务。

```python
class CronTool(Tool):
    def __init__(self, cron_service, default_timezone="UTC"):
        self.cron = cron_service

    def execute(self, action, name=None, schedule=None, message=None, **kw):
        if action == "add":
            return self.cron.add_job(
                name=name,
                schedule=CronSchedule(**schedule),
                message=message,
                deliver=kw.get("deliver", False),
                channel=kw.get("channel"),
                to=kw.get("to"),
            )
        elif action == "list":
            return self.cron.list_jobs()
        elif action == "remove":
            return self.cron.remove_job(kw["job_id"])
```

## 从 s09 到 s10 的变更

| 组件 | 之前（s09） | 之后（s10） |
|-----------|-------------|-------------|
| 智能体行为 | 纯粹反应式 | 主动式——可以主动发起行动 |
| 执行模型 | 消息驱动 | 定时器驱动 |
| 调度类型 | 无 | `at`、`every`、`cron` |
| 任务持久化 | 无 | `jobs.json` 带原子写入 |
| 定时器粒度 | 无 | 可配置的 `max_sleep_ms`（默认 5 分钟） |
| 智能体工具 | 无 | `CronTool`——模型可以创建/删除/列出任务 |
| 运行历史 | 无 | `state.run_history`（每个任务最近 20 次运行） |
| 一次性任务 | 无 | `delete_after_run` 用于提醒 |
| 热重载 | 无 | 每次计时周期调用 `_load_store()` 以获取外部变更 |
| 使用场景 | 仅反应式 | 提醒、周期性任务、定时投递 |

## 试试看

```bash
python chapters/11_cron.py
```

推荐尝试的提示词：
- `安排一个 2 分钟后的提醒，检查天气`
- `设置一个每天早上 9 点的简报任务，总结我的待办事项`
- `创建一个每 30 秒重复执行的任务，打印 "tick"——然后将其删除`

---

**设计说明：** 在参考实现中，`CronService` 是一个功能完备的调度器，具备原子存储持久化（受 fsync 保护）、外部操作合并（适用于多进程场景）、损坏文件恢复以及用于内部任务（如 Dream 记忆整合）的 `register_system_job` 功能。`CronTool` 向智能体暴露了添加/列出/删除/更新/运行等操作。心跳服务构建在相同的 cron 基础设施之上，用于周期性健康检查。