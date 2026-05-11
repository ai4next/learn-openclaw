#!/usr/bin/env python3
"""Harness layer: Tool System

    +-----------+     +-----------+     +-----------+
    |  User     | --> |  Agent    | --> |  Model    |
    |  Input    |     |  Loop     |     |  (LLM)    |
    +-----------+     +-----------+     +-----------+
                            |                |
                     +-----------+     +-----------+
                     | Tool      | <-- |  tool_use |
                     | Registry  |     |  stop_    |
                     |           |     |  reason   |
                     +-----------+     +-----------+

Key insight: Tools are defined as JSON Schema. The model reads
the schemas and calls tools by name. The harness dispatches.

Lifecycle: before_execute -> execute -> after_execute
Concurrency-safe tools can run in parallel (concurrency_safe=True).
Errors are classified: execution, permission, or system failure.
"""

import json
import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = os.path.dirname(os.path.abspath(__file__))
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Tool Error Types ────────────────────────────────────────────────────

class ToolExecutionError(Exception):
    """The tool ran but failed (e.g. file not found, command failed)."""
    pass

class ToolPermissionError(Exception):
    """The tool is not allowed to access the requested resource."""
    pass

class ToolSystemError(Exception):
    """The tool infrastructure itself failed (e.g. subprocess crashed)."""
    pass


# ── Tool ABC ──────────────────────────────────────────────────────────

class Tool(ABC):
    """Base class for all tools. Each tool defines its own JSON Schema.

    Lifecycle: before_execute -> execute -> after_execute
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """JSON Schema for tool parameters."""
        ...

    @property
    def concurrency_safe(self) -> bool:
        """If True, this tool can run in parallel with others."""
        return False

    @abstractmethod
    def execute(self, **kwargs) -> str:
        ...

    def before_execute(self, **kwargs) -> dict:
        """Pre-execution hook. Validate or transform kwargs.
        Raise ToolPermissionError to reject the call."""
        return kwargs

    def after_execute(self, result: str, **kwargs) -> str:
        """Post-execution hook. Transform or annotate the result."""
        return result

    def to_api_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class ToolRegistry:
    """Registry that maps tool names to Tool instances."""

    def __init__(self):
        self._tools = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_schemas(self) -> list[dict]:
        return [t.to_api_schema() for t in self._tools.values()]

    def execute(self, name: str, **kwargs) -> str:
        """Execute a tool with lifecycle hooks and error classification."""
        tool = self.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"

        try:
            # Phase 1: before_execute (validation)
            validated_kwargs = tool.before_execute(**kwargs)

            # Phase 2: execute
            result = tool.execute(**validated_kwargs)

            # Phase 3: after_execute (post-processing)
            return tool.after_execute(result=result, **validated_kwargs)

        except ToolPermissionError as e:
            return f"Permission denied: {e}\n(This is a hard policy boundary, do not retry with alternative approaches.)"
        except ToolExecutionError as e:
            return f"Execution error: {e}"
        except ToolSystemError as e:
            return f"System error: {e}"
        except subprocess.TimeoutExpired:
            return "Execution error: command timed out (tool system)"
        except Exception as e:
            return f"System error: unexpected failure — {e}"


# ── Tools ─────────────────────────────────────────────────────────────

class BashTool(Tool):
    @property
    def name(self): return "bash"

    @property
    def description(self): return "Execute a shell command"

    @property
    def concurrency_safe(self): return True

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
            },
            "required": ["command"],
        }

    def before_execute(self, command: str) -> dict:
        """Validate command before execution."""
        if not command or not command.strip():
            raise ToolExecutionError("command must not be empty")
        dangerous = ["rm -rf /", ":(){ :|:& };:", "dd if=/dev/zero of="]
        if any(d in command for d in dangerous):
            raise ToolPermissionError(f"command pattern not allowed: {command[:80]}")
        return {"command": command}

    def execute(self, command: str) -> str:
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"STDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\nExit code: {result.returncode}"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            raise ToolSystemError("command timed out")
        except FileNotFoundError:
            raise ToolSystemError("shell not found")
        except OSError as e:
            raise ToolSystemError(str(e))


class ReadTool(Tool):
    @property
    def name(self): return "read"

    @property
    def description(self): return "Read a file from the filesystem"

    @property
    def concurrency_safe(self): return True

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"},
            },
            "required": ["file_path"],
        }

    def before_execute(self, file_path: str) -> dict:
        path = Path(file_path).resolve()
        if not path.exists():
            raise ToolExecutionError(f"file not found: {file_path}")
        if not path.is_file():
            raise ToolExecutionError(f"not a file: {file_path}")
        return {"file_path": file_path}

    def execute(self, file_path: str) -> str:
        try:
            path = Path(file_path).resolve()
            return path.read_text(encoding="utf-8")
        except PermissionError:
            raise ToolPermissionError(f"no read permission: {file_path}")
        except Exception as e:
            raise ToolSystemError(str(e))


class WriteTool(Tool):
    @property
    def name(self): return "write"

    @property
    def description(self): return "Write content to a file"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["file_path", "content"],
        }

    def execute(self, file_path: str, content: str) -> str:
        try:
            path = Path(file_path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Written {len(content)} bytes to {file_path}"
        except PermissionError:
            raise ToolPermissionError(f"no write permission: {file_path}")
        except OSError as e:
            raise ToolSystemError(str(e))


# ── Agent Loop ────────────────────────────────────────────────────────

registry = ToolRegistry()
registry.register(BashTool())
registry.register(ReadTool())
registry.register(WriteTool())

SYSTEM_PROMPT = f"""You are an AI agent running in an interactive loop.
You have access to tools. Use them when needed.

Available tools:
{json.dumps(registry.list_schemas(), indent=2)}

Guidelines:
- Use bash to explore, read, and manipulate the filesystem
- Use read to view file contents
- Use write to create or modify files
- Think step by step. Use multiple tool calls if needed."""


def agent_loop(messages):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM_PROMPT,
            messages=messages,
            max_tokens=4096,
            tools=registry.list_schemas(),
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

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

    return response.content


def repl():
    messages = []
    print("=== OpenClaw s02: Tool System ===")
    print("Tools: bash, read, write")
    print("Type 'q' to quit\n")

    while True:
        try:
            user_input = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("q", "quit", "exit"):
            break

        messages.append({"role": "user", "content": user_input})
        content = agent_loop(messages)
        for block in content:
            if block.type == "text":
                print(block.text)


if __name__ == "__main__":
    repl()