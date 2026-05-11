# s02: Tool System


> **"Tools are JSON Schema. The model reads them and calls them by name."**
>
> Harness layer: Tool System

## Problem

An agent that can only talk is not very useful. It needs to run shell commands, read files, write code, query databases -- anything a developer can do at a terminal. But how do you give the model these capabilities without hardcoding every action into the agent loop?

The solution is a **tool abstraction**: define each capability as a named function with a JSON Schema describing its parameters. The model sees the schemas and calls tools by name. The harness dispatches the call, captures the result, and feeds it back into the loop. The agent loop itself does not know what each tool does -- it just routes `tool_use` blocks to the registry and `tool_result` blocks back to the model.

## Solution

```
                     +-----------+
                     |  Model    |
                     |  (LLM)    |
                     +-----------+
                          |
                    tool_use block
                   (name + input)
                          |
                          v
                   +-----------+
                   | Tool      |
                   | Registry  |
                   +-----------+
                    /     |    \
                   v      v     v
           +--------+ +--------+ +--------+
           | Bash   | | Read   | | Write  |
           | Tool   | | Tool   | | Tool   |
           +--------+ +--------+ +--------+
                   |      |     |
                   v      v     v
              tool_result block
                    |
                    v
              Agent Loop appends
              result to messages
                    |
                    v
              Model continues
```

The agent loop treats tool calls as data: a `tool_use` block triggers a dispatch, and the result is packaged as a `tool_result` block and appended to the conversation. The model then continues generating -- possibly calling more tools, or ending the turn.

## How It Works

### 1. Define the Tool ABC

Every tool is a subclass of `Tool` with four properties: `name`, `description`, `parameters` (JSON Schema), and an `execute` method.

```python
class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """JSON Schema for tool parameters."""
        ...

    @abstractmethod
    def execute(self, **kwargs) -> str: ...

    def to_api_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
```

The `to_api_schema()` method converts the tool definition into the format the Anthropic API expects. This is the bridge between your tool definition and the model's tool-use API.

### 2. Implement concrete tools

Each tool implements the ABC with its own schema and execution logic.

```python
class BashTool(Tool):
    @property
    def name(self): return "bash"

    @property
    def description(self): return "Execute a shell command"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
            },
            "required": ["command"],
        }

    def execute(self, command: str) -> str:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        # ... assemble and return output
```

Three tools are provided in s02:

| Tool | Purpose | Parameter |
|------|---------|-----------|
| `bash` | Execute any shell command | `command` (string) |
| `read` | Read a file from disk | `file_path` (string) |
| `write` | Write content to a file | `file_path`, `content` (strings) |

### 3. Register tools in the registry

Tools live in a `ToolRegistry` -- a simple dict-based map from tool name to tool instance.

```python
registry = ToolRegistry()
registry.register(BashTool())
registry.register(ReadTool())
registry.register(WriteTool())
```

The registry provides three operations: `register()`, `list_schemas()` (for the API call), and `execute()` (for dispatching).

### 4. Pass tool schemas to the model

The system prompt now includes the tool schemas inline, and the API call includes them in the `tools` parameter:

```python
response = client.messages.create(
    model=MODEL,
    system=SYSTEM_PROMPT + json.dumps(registry.list_schemas(), indent=2),
    messages=messages,
    max_tokens=4096,
    tools=registry.list_schemas(),  # <-- NEW
)
```

### 5. Handle tool_use in the loop

When `stop_reason == "tool_use"`, the agent iterates over content blocks, dispatches each tool call, and appends the result as a `tool_result` block. Then the loop continues -- the model sees the result and decides what to do next.

```python
if response.stop_reason == "tool_use":
    for block in response.content:
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
```

The loop does not break after tool_use -- the model may call multiple tools in sequence. The loop only breaks on `end_turn`.

### 6. Tool lifecycle hooks (enhanced)

Production tools have a three-phase lifecycle. The base class provides hooks for each phase:

```
before_execute(**kwargs)  →  validate, transform, or reject
       │
       v
execute(**kwargs)         →  perform the actual work
       │
       v
after_execute(result, **kwargs)  →  post-process or annotate
```

**`before_execute`** is called first. It can modify the arguments or raise `ToolPermissionError` to reject the call:

```python
def before_execute(self, command: str) -> dict:
    if not command.strip():
        raise ToolExecutionError("command must not be empty")
    dangerous = ["rm -rf /", ":(){ :|:& };:"]
    if any(d in command for d in dangerous):
        raise ToolPermissionError(f"pattern not allowed")
    return {"command": command}
```

**`after_execute`** is called with the result. It can transform the output:

```python
def after_execute(self, result: str, **kwargs) -> str:
    MAX = 10_000
    return result[:MAX] + "..." if len(result) > MAX else result
```

### 7. Error classification

Tools raise typed exceptions so the harness can produce model-appropriate error messages:

| Exception | Meaning | Model Response |
|-----------|---------|----------------|
| `ToolExecutionError` | Normal failure (file not found, bad input) | Try a different approach |
| `ToolPermissionError` | Hard policy boundary | Stop -- ask the user |
| `ToolSystemError` | Infrastructure failure (crash, OOM) | Retry or escalate |

The registry catches each type and formats a distinct error message. Permission errors include a "hard policy boundary" note that teaches the model not to retry with tricks:

```python
except ToolPermissionError as e:
    return f"Permission denied: {e}\n(This is a hard policy boundary, do not retry with alternative approaches.)"
```

### 8. Concurrency-safe flag

Tools declare whether they can run in parallel:

```python
@property
def concurrency_safe(self): return True  # ReadTool, BashTool
```

This is a hint to the harness. In later sessions (s07+), concurrent tools will be dispatched in parallel for faster execution.

## What Changed From s01

| Component | Before (s01) | After (s02) |
|-----------|--------------|-------------|
| Tool handling | None -- text-only agent | `Tool` ABC with JSON Schema; `ToolRegistry` for dispatch |
| Tool lifecycle | N/A | Three-phase: `before_execute → execute → after_execute` |
| Error handling | Uncaught exceptions crash the loop | Classified errors: `ToolExecutionError`, `ToolPermissionError`, `ToolSystemError` |
| Concurrency | N/A | `concurrency_safe` property hints at parallel dispatch |
| LLM API call | No `tools` parameter | `tools=registry.list_schemas()` included |
| stop_reason handling | Only checks `end_turn` | Checks both `end_turn` and `tool_use` |
| Tool result feedback | N/A | `tool_result` blocks appended to messages |
| System prompt | Static text | Includes tool schemas in system prompt |
| Agent capabilities | Conversation only | Can run bash, read files, write files |

## Try It

```bash
python chapters/02_tool_system.py
```

Suggested prompts:

- "Create a file called hello.txt with the text 'Hello, OpenClaw!'"
- "Read the contents of /etc/hostname and tell me the hostname."
- "List all Python files in the current directory and show me their sizes."
- "Write a Python script that prints the Fibonacci sequence, then run it."

## Extensibility

Adding a new tool requires exactly three things:

1. Create a new class inheriting from `Tool`
2. Define `name`, `description`, `parameters`, and `execute`
3. Register it: `registry.register(MyNewTool())`

New tools get automatic JSON Schema generation, automatic inclusion in the API call, and automatic dispatch. The agent loop and the rest of the harness do not change.