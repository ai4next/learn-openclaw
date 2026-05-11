[English](./README.md) | [简体中文](./README-zh.md)

# Learn OpenClaw — Build an Open-Source AI Agent from Scratch

> **Agency comes from the model, not from external code. The harness is the vehicle; the model is the driver.**

**Learn OpenClaw** is a progressive, hands-on tutorial for understanding and building an [OpenClaw](https://github.com/openclaw)-style AI agent framework — the same architecture that powers production-grade, multi-platform AI agents. You will start with a 20-line loop and end with a full-featured agent that has tools, skills, memory, channels, cron, subagents, and more.

**small core, extended at the edges.**

## Why OpenClaw?

Most "AI agent frameworks" are Rube Goldberg machines — prompt plumbing disguised as infrastructure. OpenClaw takes a different approach:

- **The model is the agent.** The core loop is `while True` — the model decides what to do next via `stop_reason`.
- **The harness enables, not controls.** Tools, skills, memory, and channels are passive resources the model chooses to use.
- **Core stays small; extend at the edges.** The agent loop is intentionally minimal. New capabilities go into tools, skills, or channels — not into the core runtime.
- **Prefer duplication over premature abstraction.** Channels and subsystems may repeat patterns rather than share complex base classes.

This project implements the **harness** — the environment, tools, knowledge, and interfaces that make the model effective.

## Learning Path (14 Sessions / 5 Phases)

### Phase 1: The Core
| Session | Topic | What You Build |
|---------|-------|----------------|
| s01 | **Agent Loop** | The foundational `while True` + `stop_reason` loop |
| s02 | **Tool System** | Tool ABC with JSON Schema, lifecycle hooks (`before_execute`/`after_execute`), error classification |

### Phase 2: Intelligence
| Session | Topic | What You Build |
|---------|-------|----------------|
| s03 | **Skills System** | YAML frontmatter-driven knowledge injection with on-demand loading |
| s04 | **Memory System** | File-based persistent memory with token-budget Consolidator → `history.jsonl` |
| s05 | **Context Management** | Token budgeting, tiered compaction engine, AutoCompact |

### Phase 3: Communication
| Session | Topic | What You Build |
|---------|-------|----------------|
| s06 | **Message Bus** | Async inbound/outbound queues with PendingQueue for mid-turn injection |
| s07 | **Session Management** | JSONL-based persistence with RuntimeCheckpoint crash recovery |
| s08 | **Channel System** | Multi-platform routing (CLI, File, Log, WebSocket) |

### Phase 4: Production
| Session | Topic | What You Build |
|---------|-------|----------------|
| s09 | **Configuration** | Declarative config with Pydantic-style models and `${ENV_VAR}` resolution |
| s10 | **Cron & Scheduling** | Scheduled autonomous operation with HeartbeatService |
| s11 | **Gateway & API** | HTTP service with OpenAI-compatible API |

### Phase 5: Advanced
| Session | Topic | What You Build |
|---------|-------|----------------|
| s12 | **Security & Sandbox** | Workspace path restriction, SSRF protection, shell sandbox |
| s13 | **Subagent System** | Spawn child agents, parallel execution, mid-turn injection |
| s14 | **Dream Processor** | Two-phase background memory consolidation (analyze + edit) |

### Capstone
| File | What It Is |
|------|------------|
| `complete.py` | All 14 sessions combined into one integrated agent |

## Quick Start

```bash
# Clone and enter
git clone <this-repo>
cd learn-openclaw

# Install dependencies
pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# Run session 1 — the agent loop
python chapters/01_agent_loop.py
# Type any question at the s01 >> prompt
# Type 'q' to quit
```

Each session is self-contained and runnable. Start with s01 and work your way forward. Each session's doc explains what changed and why.

## Structure

```
learn-openclaw/
├── chapters/          # Runnable Python chapter files (01–14 + complete.py)
├── docs/
│   ├── en/          # English tutorial docs (s01–s14)
│   └── zh/          # 简体中文教程文档 (s01–s14)
├── README.md        # This file
└── requirements.txt # Dependencies
```

## Design Principles

> **The loop is sacred. Never change it. Layer mechanisms around it.**

1. **Trust the model.** Given the right tools and knowledge, the model will make good decisions. Don't second-guess it with rigid flows.
2. **Harness, not prompt.** Build the environment and tools. Let the model figure out the plan.
3. **Progressive complexity.** Start minimal. Add one mechanism at a time. Each session is a single, understandable delta.
4. **Self-contained sessions.** Each agent file works standalone. No shared state between sessions.
5. **Bash is all you need.** A shell tool gives the model unlimited power. Everything else is ergonomics.

## What's Covered

| Topic | Session |
|-------|---------|
| Agent loop with `stop_reason` | s01 |
| Tool lifecycle hooks & error classification | s02 |
| YAML-frontmatter skills with on-demand loading | s03 |
| Memory store + Consolidator (`history.jsonl`) | s04 |
| Token budgeting + tiered compaction + AutoCompact | s05 |
| Message bus with mid-turn injection queue | s06 |
| JSONL session persistence + RuntimeCheckpoint | s07 |
| Multi-channel routing (CLI, File, Log, WebSocket) | s08 |
| Pydantic-style config with `${ENV_VAR}` resolution | s09 |
| Cron scheduling + HeartbeatService | s10 |
| HTTP gateway with OpenAI-compatible API | s11 |
| Workspace restriction + SSRF protection + sandbox | s12 |
| Subagent spawn/collect with parallel execution | s13 |
| Two-phase Dream memory consolidation | s14 |

## What This Project Does NOT Cover

- Full event hooks and lifecycle callbacks
- Permission governance and approval workflows
- Cross-instance session replication
- Production deployment orchestration

These are important topics, but they belong in a production framework, not a learning project.

## License

[MIT](./LICENSE)