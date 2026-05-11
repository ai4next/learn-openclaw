# s05: Context Management


> **"Context windows are finite. The harness manages what fits."**
>
> Harness layer: Context Governance

## Problem

Every message the agent exchanges -- user input, assistant responses, tool results, memory content, skill bodies -- accumulates in the `messages` list. LLM context windows are finite (typically 8K to 200K tokens). A long-running session will eventually exceed the limit, causing errors, degraded performance, or silently truncated context.

Simply trimming the oldest messages is naive: an old message might contain critical context (the user's name, a key decision, a file path being edited). The agent needs **intelligent context management**: token budgeting, selective pruning, automatic compaction, and structural awareness of what can be safely removed.

## Solution

```
                     +-----------+
                     |  Token    |
                     |  Counter  |
                     +-----------+
                           |
         +-----------------+-----------------+
         |                                    |
         v                                    v
+--------------------+             +--------------------+
| Budget             |             | Compaction         |
| Enforcement        |             | Engine             |
| (before each call) |             | (when budget       |
|                    |             |  exceeded)         |
+--------------------+             +--------------------+
         |                                    |
         v                                    v
+-----------------------------------+  +--------------------+
| 1. Count current tokens           |  | 1. Summarize oldest|
| 2. Estimate space for new turn    |  |    non-essential   |
| 3. If over budget, trigger        |  |    messages        |
|    compaction                     |  | 2. Remove tool_use |
| 4. Proceed with LLM call          |  |    chains          |
+-----------------------------------+  | 3. Condense system |
                                       |    prompt          |
                                       +--------------------+
```

The context manager sits between the agent loop and the LLM call. Before each request, it counts tokens, estimates the budget, and triggers compaction if needed. Compaction produces a shorter but semantically equivalent conversation history.

## How It Works

### 1. Token counting

A `TokenCounter` wraps a tiktoken encoder (or Anthropic tokenizer) to count tokens in messages without sending them to the API. This is critical -- you need to know the budget before you make the call.

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

### 2. Define the budget

The context budget defines how many tokens the agent can use. A portion is reserved for the upcoming response:

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

### 3. Pruning strategy: what to remove first

Not all messages are equally valuable. The `CompactionEngine` applies a tiered pruning strategy:

| Priority | What | Why |
|----------|------|-----|
| **Remove first** | `tool_use` / `tool_result` chains from resolved tasks | These are already done; only the final result matters |
| **Remove next** | Old assistant responses the user has seen | The user already read them; only the gist is relevant |
| **Summarize** | Long conversation segments | Replace 10 turns with a 3-sentence summary |
| **Condense** | System prompt segments | Remove duplicate instructions, inline only active skills |
| **Keep always** | User preferences, decisions, critical context | These define the agent's understanding of the current task |

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

### 4. Integration with the agent loop

The agent loop calls the `ContextManager` before each LLM request:

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

### 5. System prompt condensation

The system prompt itself can grow large (skill bodies, memory content, tool descriptions). The context manager condenses it separately:

- Static parts (identity, rules) are kept verbatim.
- Dynamic parts (skill bodies, memory) are truncated or replaced with summaries.
- Tool schemas remain intact (they are small and critical).

## What Changed From s05

| Component | Before (s04) | After (s05) |
|-----------|--------------|-------------|
| Context awareness | None -- messages grow unbounded | `TokenCounter` measures token usage before each call |
| Budget enforcement | No limits | `ContextBudget` with ceiling, response reservation, compaction threshold |
| Compaction strategy | Consolidator archives old turns to `history.jsonl` | `CompactionEngine` with tiered pruning: tool chains, old turns, summarization |
| AutoCompact | None | TTL-based session expiry detection with automatic compaction |
| Token counting | `estimate_tokens()` 4-char heuristic | `TokenCounter.count_messages()` with content_block list support |
| System prompt management | Static or manual updates | `prepare_system_prompt()` truncates to budget |
| Message preparation | Raw messages passed to API | `prepare_messages()` compacts before each LLM call |
| Long-session support | Degraded quality or crashes | Graceful compaction + AutoCompact enables extended sessions |

## Try It

```bash
python chapters/06_context.py
```

Suggested prompts:

- "Tell me a long story, one paragraph at a time. I'll say 'continue' between each one." (Simulate a long session, then observe compaction)
- "What messages do you still remember from the beginning of our conversation?"
- "Create 10 files with random content, then read them all back to me." (Generates many tool_use/tool_result pairs to trigger compaction)

## Compaction Trade-offs

| Strategy | Information Loss | Token Savings | Complexity |
|----------|-----------------|---------------|------------|
| Remove old tool chains | Low (results were consumed) | High | Low |
| Truncate old messages | Medium (loses nuance) | High | Low |
| Summarize segments | Medium-Low (LLM preserves gist) | High | High |
| Truncate system prompt | Medium (loses edge details) | Medium | Low |

The compaction engine applies strategies in order of increasing information loss. The key principle: **preserve the semantic core of the conversation** while shedding redundant or consumed detail. The compaction is intentionally conservative -- it is better to let a long message through than to accidentally remove critical context.

### 6. AutoCompact — TTL-based session expiry

Beyond per-turn compaction, production agents need **AutoCompact**: automatic detection of idle sessions and behind-the-scenes cleanup. When a session has been inactive past its TTL (time-to-live), older messages are compacted before the next turn:

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

Integrated into the REPL loop between turns:

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

This pattern is used in production agent frameworks, where `AutoCompact` runs between messages in the agent loop, keeping long-lived sessions tidy without user intervention.