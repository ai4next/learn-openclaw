# s10: Cron & Scheduling


> **"An agent that only responds is half an agent."**
> Harness layer: Autonomous Operation

## Problem

Through s10, the agent is purely reactive. It waits for a message, processes it, responds, and waits again. It can never initiate action on its own. There is no way to say "check the weather every morning at 8 AM" or "send me a weekly summary" or "remind me about X in 2 hours."

A reactive agent is always on call but never proactive. Real-world agents need to:
- Run periodic maintenance (memory consolidation, heartbeat pings)
- Deliver scheduled notifications (morning briefings, deadline reminders)
- Execute one-shot delayed tasks ("remind me in 30 minutes")
- Operate autonomously even when nobody is talking to them

## Solution

Add a **CronService** that manages a persistent list of scheduled jobs. Each job has a schedule (cron expression, fixed interval, or one-shot timestamp) and a payload (message to process). The service runs a timer loop that wakes up when a job is due, executes it, and schedules the next wake.

```
CronService lifecycle:

  start()
    -> _load_store()            # load jobs from jobs.json
    -> _recompute_next_runs()   # calculate next timestamps
    -> _arm_timer()             # schedule asyncio.sleep

  _on_timer()
    -> _load_store()            # hot-reload if file changed
    -> find due_jobs
    -> for each: _execute_job()
    -> _save_store()            # persist run history
    -> _arm_timer()             # schedule next wake

Timer management:

                        now                          next job
  time:  ----|-----------|-----------------------------|----------->
              \           \                             \
          _arm_timer()   asyncio.sleep(delay)         _on_timer()
                                                         \
                                                   _execute_job()
                                                         \
                                                   _arm_timer()

Schedule types:
  "every":  every N milliseconds              [---interval---]
  "cron":   cron expression (e.g. "0 9 * * *") [daily at 9am]
  "at":     one-shot at specific timestamp      [...X]
```

Jobs are persisted to `jobs.json` with atomic writes (same pattern as session files). The service survives restarts -- missed jobs run on the next timer tick.

## How It Works

1. **CronSchedule and CronJob dataclasses** define what to run and when.

```python
@dataclass
class CronSchedule:
    kind: Literal["at", "every", "cron"]
    at_ms: int | None = None       # for "at": absolute timestamp in ms
    every_ms: int | None = None    # for "every": interval in ms
    expr: str | None = None        # for "cron": "0 9 * * *"
    tz: str | None = None          # timezone for cron expressions

@dataclass
class CronPayload:
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    deliver: bool = False          # send response to channel
    channel: str | None = None     # e.g. "telegram"
    to: str | None = None          # e.g. chat_id
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

2. **Next-run computation** translates schedules into absolute timestamps.

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

3. **Timer loop** wakes at the earliest due time, executes jobs, and re-arms.

```python
class CronService:
    def __init__(self, store_path: Path, on_job=None, max_sleep_ms=300_000):
        self.store_path = store_path
        self.on_job = on_job  # callback: async def on_job(job) -> str|None
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
        self._load_store()  # hot-reload
        now = _now_ms()
        due_jobs = [j for j in self._store.jobs
                    if j.enabled and j.state.next_run_at_ms
                    and now >= j.state.next_run_at_ms]
        for job in due_jobs:
            await self._execute_job(job)
        self._save_store()
        self._arm_timer()
```

4. **Job execution** calls the `on_job` callback (which routes to the agent loop).

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

    # One-shot: disable or delete
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

5. **Agent tool integration** lets the model create cron jobs at runtime.

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

## What Changed From s10

| Component | Before (s09) | After (s10) |
|-----------|-------------|-------------|
| Agent behavior | Purely reactive | Proactive -- can initiate actions |
| Execution model | Message-driven | Timer-driven |
| Schedule types | None | `at`, `every`, `cron` |
| Job persistence | None | `jobs.json` with atomic writes |
| Timer granularity | N/A | Configurable `max_sleep_ms` (default 5min) |
| Agent tool | None | `CronTool` -- model can create/remove/list jobs |
| Run history | N/A | `state.run_history` (last 20 runs per job) |
| One-shot jobs | N/A | `delete_after_run` for reminders |
| Hot-reload | N/A | `_load_store()` on each tick picks up external changes |
| Use cases | Reactive only | Reminders, periodic tasks, scheduled delivery |

## Try It

```bash
python chapters/11_cron.py
```

Suggested prompts:
- `Schedule a reminder for 2 minutes from now to check the weather`
- `Set up a daily 9 AM briefing job that summarizes my tasks`
- `Create a repeating job every 30 seconds that prints "tick" -- then remove it`

---

**Design Note:** In the reference implementation, `CronService` is a full-featured scheduler with atomic store persistence (fsync-protected), external action merging (for multi-process scenarios), corrupt-file recovery, and `register_system_job` for internal tasks like Dream memory consolidation. The `CronTool` exposes add/list/remove/update/run operations to the agent. The heartbeat service is built on top of the same cron infrastructure for periodic health checks.