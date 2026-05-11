#!/usr/bin/env python3
"""Harness layer: Configuration

    +-----------+
    | config.   |  JSON file on disk
    | json      |
    +-----------+
         |
    +-----------+
    | Config    |  Pydantic models
    | Schema    |  Validation + defaults
    +-----------+
         |
    +-----------+
    | Provider  |  Model, API key, base URL
    | Config    |
    +-----------+

Key insight: Configuration is data, not code. Models validate;
env vars override; sensible defaults fill gaps.
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Minimal Pydantic-like Validation ────────────────────────────────────
# We use dataclasses with a validate() method so there's no hard
# dependency on pydantic. The pattern mirrors Pydantic's approach.

class ValidationError(Exception):
    pass


class Field:
    """Simple field descriptor with type checking and defaults."""

    def __init__(self, type_, default=None, description=""):
        self.type = type_
        self.default = default
        self.description = description

    def validate(self, value, name):
        if value is None:
            return self.default
        if self.type is not None and not isinstance(value, self.type):
            raise ValidationError(
                f"'{name}': expected {self.type.__name__}, got {type(value).__name__}"
            )
        return value


class ConfigMeta(type):
    """Metaclass that collects Field definitions."""

    def __new__(mcs, name, bases, namespace):
        fields = {}
        for key, val in list(namespace.items()):
            if isinstance(val, Field):
                fields[key] = val
        cls = super().__new__(mcs, name, bases, namespace)
        cls._fields = fields
        return cls


# ── Config Models ───────────────────────────────────────────────────────

class ProviderConfig(metaclass=ConfigMeta):
    model = Field(str, "claude-sonnet-4-6")
    api_key = Field(str, "${ANTHROPIC_API_KEY}")
    base_url = Field(str, "https://api.anthropic.com/v1")
    max_tokens = Field(int, 4096)

    def __init__(self, **kwargs):
        for name, field in self._fields.items():
            setattr(self, name, field.validate(kwargs.get(name), name))

    def to_dict(self):
        return {name: getattr(self, name) for name in self._fields}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls._fields})


class AgentConfig(metaclass=ConfigMeta):
    system_prompt = Field(str, "You are an OpenClaw AI agent.")
    tools_enabled = Field(bool, True)
    memory_enabled = Field(bool, False)
    max_context_size = Field(int, 8000)

    def __init__(self, **kwargs):
        for name, field in self._fields.items():
            setattr(self, name, field.validate(kwargs.get(name), name))

    def to_dict(self):
        return {name: getattr(self, name) for name in self._fields}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls._fields})


class ChannelConfig(metaclass=ConfigMeta):
    name = Field(str, "cli")
    enabled = Field(bool, True)
    settings = Field(dict, {})

    def __init__(self, **kwargs):
        for name, field in self._fields.items():
            setattr(self, name, field.validate(kwargs.get(name), name))

    def to_dict(self):
        return {name: getattr(self, name) for name in self._fields}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls._fields})


class AppConfig(metaclass=ConfigMeta):
    provider = Field(dict, {})
    agent = Field(dict, {})
    channels = Field(list, [])
    workspace = Field(str, str(WORKDIR))

    def __init__(self, **kwargs):
        for name, field in self._fields.items():
            value = kwargs.get(name, field.default)
            setattr(self, name, value)

    @property
    def provider_config(self) -> ProviderConfig:
        return ProviderConfig.from_dict(self.provider if isinstance(self.provider, dict) else {})

    @property
    def agent_config(self) -> AgentConfig:
        return AgentConfig.from_dict(self.agent if isinstance(self.agent, dict) else {})

    @property
    def channel_configs(self) -> list[ChannelConfig]:
        items = self.channels if isinstance(self.channels, list) else []
        return [ChannelConfig.from_dict(c) if isinstance(c, dict) else c for c in items]

    def to_dict(self):
        return {
            "provider": self.provider_config.to_dict(),
            "agent": self.agent_config.to_dict(),
            "channels": [c.to_dict() for c in self.channel_configs],
            "workspace": self.workspace,
        }


# ── Config Loader ───────────────────────────────────────────────────────

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


class ConfigLoader:
    """Loads, validates, and saves configuration."""

    @staticmethod
    def load(path: Path) -> AppConfig:
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig(**data)

    @staticmethod
    def load_with_env(config: AppConfig) -> AppConfig:
        """Resolve ${VAR} patterns in string fields using environment variables."""
        resolved = config.to_dict()
        for section in ("provider", "agent"):
            if section in resolved and isinstance(resolved[section], dict):
                for key, value in resolved[section].items():
                    if isinstance(value, str):
                        resolved[section][key] = ConfigLoader._resolve_env(value)
        return AppConfig(**resolved)

    @staticmethod
    def _resolve_env(value: str) -> str:
        def _replace(match):
            var = match.group(1)
            return os.getenv(var, "")
        return _ENV_VAR_PATTERN.sub(_replace, value)

    @staticmethod
    def save(config: AppConfig, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(config.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )


# ── Sample Config ───────────────────────────────────────────────────────

SAMPLE_CONFIG = {
    "provider": {
        "model": "claude-sonnet-4-6",
        "api_key": "${ANTHROPIC_API_KEY}",
        "base_url": "https://api.anthropic.com/v1",
        "max_tokens": 4096,
    },
    "agent": {
        "system_prompt": "You are an OpenClaw AI agent. Use tools when needed.",
        "tools_enabled": True,
        "memory_enabled": False,
        "max_context_size": 8000,
    },
    "channels": [
        {"name": "cli", "enabled": True, "settings": {}},
        {"name": "log", "enabled": True, "settings": {}},
    ],
    "workspace": str(WORKDIR),
}


# ── Tools ───────────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self):
        self._tools = {}
    def register(self, tool):
        self._tools[tool.name] = tool
    def list_schemas(self):
        return [t.to_api_schema() for t in self._tools.values()]
    def execute(self, name, **kwargs):
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        return tool.execute(**kwargs)

class Tool:
    @property
    def name(self): raise NotImplementedError
    @property
    def description(self): raise NotImplementedError
    @property
    def parameters(self): raise NotImplementedError
    def to_api_schema(self):
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}
    def execute(self, **kwargs): raise NotImplementedError

class BashTool(Tool):
    name = "bash"
    description = "Execute a shell command"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    def execute(self, command=""):
        import subprocess
        try:
            r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            out = r.stdout or ""
            if r.stderr: out += f"\nSTDERR:\n{r.stderr}"
            if r.returncode != 0: out += f"\nExit code: {r.returncode}"
            return out or "(no output)"
        except Exception as e: return f"Error: {e}"

class ReadTool(Tool):
    name = "read"
    description = "Read a file"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    def execute(self, file_path=""):
        try: return Path(file_path).resolve().read_text(encoding="utf-8")
        except Exception as e: return f"Error: {e}"

class WriteTool(Tool):
    name = "write"
    description = "Write content to a file"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}
    def execute(self, file_path="", content=""):
        try:
            p = Path(file_path).resolve(); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8"); return f"Written {len(content)} bytes"
        except Exception as e: return f"Error: {e}"

class ReloadConfigTool(Tool):
    """Tool that reloads the configuration from disk."""
    def __init__(self, loader: ConfigLoader, path: Path):
        self._loader = loader
        self._path = path
    @property
    def name(self): return "reload_config"
    @property
    def description(self): return "Reload configuration from disk"
    @property
    def parameters(self):
        return {"type": "object", "properties": {}, "required": []}
    def execute(self):
        try:
            cfg = self._loader.load(self._path)
            resolved = self._loader.load_with_env(cfg)
            return f"Config reloaded: {json.dumps(resolved.to_dict(), indent=2)}"
        except Exception as e:
            return f"Error reloading config: {e}"


# ── Setup ───────────────────────────────────────────────────────────────

config_path = WORKDIR.parent / "config.json"
if not config_path.exists():
    config_path.write_text(json.dumps(SAMPLE_CONFIG, indent=2), encoding="utf-8")
    print(f"Created sample config: {config_path}")

loader = ConfigLoader()
app_config = loader.load(config_path)
resolved_config = loader.load_with_env(app_config)

registry = ToolRegistry()
for t in [BashTool(), ReadTool(), WriteTool(), ReloadConfigTool(loader, config_path)]:
    registry.register(t)

SYSTEM_PROMPT = f"""You are an OpenClaw agent with declarative configuration.

Current config:
{json.dumps(resolved_config.to_dict(), indent=2)}

Use reload_config to reload config from disk after making changes."""


# ── Agent Loop ──────────────────────────────────────────────────────────

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
                        "content": [{"type": "tool_result", "tool_use_id": block.id, "content": result}],
                    })
    return response.content


# ── REPL ────────────────────────────────────────────────────────────────

def repl():
    messages = []
    print("=== OpenClaw s09: Configuration ===")
    print(f"Config file: {config_path}")
    print("Type 'q' to quit.")
    print("Type 'config' to show current config.")
    print("Type 'path' to show config file path.\n")

    while True:
        try:
            user_input = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("q", "quit", "exit"):
            break

        if user_input.lower() == "config":
            print(json.dumps(resolved_config.to_dict(), indent=2))
            continue

        if user_input.lower() == "path":
            print(f"Config path: {config_path}")
            continue

        messages.append({"role": "user", "content": user_input})
        content = agent_loop(messages)
        for block in content:
            if block.type == "text":
                print(block.text)


if __name__ == "__main__":
    repl()