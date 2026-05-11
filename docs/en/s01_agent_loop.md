# s01: The Agent Loop


> **"The loop is sacred. Never change it. Layer mechanisms around it."**
>
> Harness layer: Core Loop

## Problem

How do you build an agent that can carry on a conversation, respond to requests, and know when to stop -- without hardcoding every possible flow? Most naive implementations treat the LLM call as a one-shot: send a prompt, get an answer, done. But real agents need to handle multi-turn reasoning, tool use, and conditional continuation, all with the same simple core.

The answer is a `while True` loop driven by the model's own `stop_reason` signal. The model decides whether the turn is finished, not the harness.

## Solution

```
                +-----------+     +-----------+
                |  Agent    |     |  Model    |
+-----------+   |  Loop     |     |  (LLM)    |
|  User     |-->|  while    |-->|           |
|  Input    |   |  True     |     |           |
+-----------+   +-----------+     +-----------+
                      |                 |
                      |          +-----------+
                      |          |  stop_    |
                      |          |  reason?  |
                      |          +-----------+
                      |           |         |
                      |       end_turn   continue
                      |           |         |
                      |        +-----+   +-----+
                      |        | send|   | loop|
                      |        | to  |   | back|
                      |        | user|   |     |
                      |        +-----+   +-----+
                      |                      |
                      |            (future: tool_use)
                      v
```

The model generates content. If `stop_reason == "end_turn"`, the response is returned to the user. Any other stop reason means the loop continues -- the model was interrupted and needs another iteration to complete its work.

## How It Works

### 1. User sends a message

The REPL accepts input via `input()` and appends it to the conversation history as a `"user"` role message.

```python
messages.append({"role": "user", "content": user_input})
```

### 2. Agent loop sends conversation to the LLM

The `agent_loop()` function calls the Anthropic SDK with the full message history. There is no hidden state -- everything the model needs is in `messages`.

```python
response = client.messages.create(
    model=MODEL,
    system=SYSTEM_PROMPT,
    messages=messages,
    max_tokens=4096,
)
```

### 3. LLM responds with content blocks

The model returns one or more content blocks (usually `text` blocks, but later sessions add `tool_use` blocks). The agent appends the entire response to the message history.

```python
messages.append({"role": "assistant", "content": response.content})
```

### 4. Check stop_reason

The `stop_reason` field tells the agent why the model stopped generating:

- **`"end_turn"`** -- the model has finished its response. Break the loop.
- **`"tool_use"`** -- the model wants to use a tool. (Handled starting in s02.)
- **`"max_tokens"`** -- the model hit the token limit. The loop continues to let it finish.

```python
if response.stop_reason == "end_turn":
    break
```

### 5. Return and display

Once the loop breaks, the final content blocks are printed to the user. The REPL then waits for the next user input.

```python
for block in content:
    if block.type == "text":
        print(block.text)
```

### 6. Repeat until the user quits

The outer REPL loop continues until the user types `q`, `quit`, or `exit`.

## What Changed From s00 (Baseline)

| Component | Before (Baseline) | After (s01) |
|-----------|--------------------|-------------|
| Architecture | No agent -- raw SDK call | `while True` loop with `stop_reason` check |
| Message handling | No conversation history | Messages list accumulates user + assistant turns |
| Loop control | Hardcoded single response | Model-driven via `stop_reason` |
| User interaction | None | `input()`-based REPL with prompt |
| Model selection | None | Configurable via `MODEL_ID` env var |
| System prompt | None | Initial system prompt defines agent behavior |

## Try It

```bash
python chapters/01_agent_loop.py
```

Suggested prompts:

- "What is the capital of France?"
- "Explain the concept of a while loop in Python."
- "Write a short poem about artificial intelligence."
- "What is 42 * 37? Show your work step by step."

## The State Machine Evolution

The `while True` + `stop_reason` loop is the simplest and most elegant foundation. As the agent grows more complex — adding tools, memory, skills, channels — this simple loop naturally evolves into a **formal state machine**. Each state handles one concern, and transitions are driven by explicit events:

```
RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE
```

| State | What It Does |
|-------|-------------|
| RESTORE | Extract documents from media, restore interrupted-turn checkpoints |
| COMPACT | Auto-compact expired sessions, archive old messages |
| COMMAND | Check for slash commands; dispatch early if matched |
| BUILD | Load skills, memory, history; build the full prompt |
| RUN | Execute the multi-turn LLM conversation (tool calls + responses) |
| SAVE | Persist the completed turn, enforce file caps, trigger consolidation |
| RESPOND | Assemble the outbound response and deliver it |
| DONE | Turn complete |

This pattern appears in production agent frameworks, where the state machine drives the entire agent loop with clear separation of concerns. Each state is a single method; transitions are a lookup table. The model never sees the state machine — it's purely a harness-level organizational pattern.

You'll see the first hint of this evolution in s02, where `tool_use` becomes a branching condition. By s07 the loop will need its first formal state separation.

## Key Design Decision

The loop is deliberately minimal. It does not branch on `tool_use` yet -- that comes in s02. It does not abstract the provider -- that comes in s03. Each session layers exactly one new mechanism on top of the previous one.

The most important insight: **the model is the agent.** The harness does not decide what to do next. The harness provides the loop and the environment; the model decides when it is done via `stop_reason`. This pattern repeats through every subsequent session.

## Implementation Notes

- The `repl()` function is the outer loop (session lifecycle). The `agent_loop()` function is the inner loop (turn completion).
- Messages accumulate across turns. Over time this will create context window pressure -- s06 addresses this.
- Only Anthropic is supported. The SDK is imported directly (`from anthropic import Anthropic`). This coupling is resolved in s03.