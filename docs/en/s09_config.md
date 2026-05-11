# s09: Configuration


> **"Configuration is data, not code."**
> Harness layer: Declarative Config

## Problem

Through s09, every parameter is hardcoded: the model name, API keys, channel tokens, tool settings, workspace path. To change the model from Claude to GPT-4o, you edit the Python file. To add a Telegram bot token, you edit the channel class. Configuration lives in source code, which means:

- Every config change requires modifying and redeploying code
- Different environments (dev, staging, production) cannot share the same binary
- Secrets (API keys, tokens) are mixed with application logic
- There is no single source of truth for what the agent is configured to do

## Solution

Move all configuration into a **single JSON file** (e.g. `config.json`), loaded at startup and validated against a **Pydantic schema**. The schema defines every knob with types, defaults, and documentation. The application code reads from a validated `Config` object, never from raw environment variables.

```
config.json:

{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5",
      "maxTokens": 8192,
      "temperature": 0.1,
      "workspace": "~/.openclaw/workspace",
      "timezone": "America/New_York"
    }
  },
  "channels": {
    "sendProgress": true,
    "sendMaxRetries": 3,
    "telegram": {
      "enabled": true,
      "botToken": "${TELEGRAM_BOT_TOKEN}",
      "allowFrom": ["*"]
    },
    "discord": {
      "enabled": true,
      "botToken": "${DISCORD_BOT_TOKEN}",
      "allowFrom": ["123456789"]
    }
  },
  "providers": {
    "anthropic": {
      "apiKey": "${ANTHROPIC_API_KEY}"
    },
    "openai": {
      "apiKey": "${OPENAI_API_KEY}"
    }
  },
  "tools": {
    "web": { "enable": true },
    "exec": { "enable": true, "timeout": 60 }
  },
  "api": {
    "host": "127.0.0.1",
    "port": 8900
  }
}

Loading flow:

  config.json  -->  load_config()  -->  Config (Pydantic)  -->  AgentLoop.from_config()
       |                 |                      |                        |
  config.json       validates              env vars               creates actual
  config.json       JSON schema            resolved               objects
```

The config is:
- **Declarative** -- a JSON file describes what the agent should be
- **Validated** -- Pydantic catches typos, wrong types, and missing fields at startup
- **Extensible** -- channel configs use `extra="allow"` so plugins add their own sections
- **Env-aware** -- `${VAR}` references resolve from environment variables (secrets stay out of the file)

## How It Works

1. **Pydantic schema** defines every config option with types, defaults, and validation.

```python
class AgentDefaults(Base):
    workspace: str = "~/.openclaw/workspace"
    model: str = "anthropic/claude-opus-4-5"
    max_tokens: int = 8192
    temperature: float = 0.1
    context_window_tokens: int = 65_536
    timezone: str = "UTC"
    unified_session: bool = False
    session_ttl_minutes: int = 0  # 0 = disabled

class ChannelsConfig(Base):
    model_config = ConfigDict(extra="allow")  # plugins add their own keys
    send_progress: bool = True
    send_max_retries: int = Field(default=3, ge=0, le=10)
    transcription_provider: str = "groq"

class Config(BaseSettings):
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def workspace_path(self) -> Path:
        return Path(self.agents.defaults.workspace).expanduser()
```

2. **Config loader** reads the JSON file and validates it against the schema.

```python
def load_config(config_path: Path | None = None) -> Config:
    path = config_path or get_config_path()  # e.g. ~/.openclaw/config.json
    config = Config()  # start with defaults

    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = _migrate_config(data)  # handle old formats
        config = Config.model_validate(data)  # Pydantic validation

    _apply_ssrf_whitelist(config)  # post-load setup
    return config
```

3. **Environment variable resolution** allows `${VAR}` references in the JSON.

```python
_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

def resolve_config_env_vars(config: Config) -> Config:
    return _resolve_in_place(config)

def _resolve_in_place(obj):
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, BaseModel):
        updates = {
            name: _resolve_in_place(getattr(obj, name))
            for name in type(obj).model_fields
        }
        return obj.model_copy(update=updates) if any(...) else obj
    # ... handles dicts, lists recursively

def _env_replace(match):
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value
```

This means `"botToken": "${TELEGRAM_BOT_TOKEN}"` in the JSON file loads the token from the environment -- keeping secrets out of version control.

4. **Config-driven agent construction** -- `AgentLoop.from_config()` builds everything from the config object.

```python
class AgentLoop:
    @classmethod
    def from_config(cls, config, bus=None, **extra):
        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = make_provider(config)
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=defaults.model,
            max_iterations=defaults.max_tool_iterations,
            context_window_tokens=defaults.context_window_tokens,
            web_config=config.tools.web,
            exec_config=config.tools.exec,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            **extra,
        )
```

5. **camelCase aliases** let the JSON use JavaScript convention while Python code uses snake_case.

```python
class Base(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
```

So `{"maxTokens": 8192}` in JSON maps to `max_tokens` in Python code transparently.

## Config Validation

Pydantic models provide validation at load time. This catches configuration errors before they cause runtime failures:

```python
class AgentConfig(BaseModel):
    model: str = Field(default="claude-sonnet-4-6", min_length=1)
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

# Loading validates immediately:
try:
    config = AppConfig(**loaded_data)
except ValidationError as e:
    print(f"Configuration error: {e}")
    sys.exit(1)
```

Key validation patterns:

- **Field constraints**: `ge=1, le=200000` bounds token values
- **Type checks**: Pydantic auto-coerces types and rejects invalid ones
- **Alias normalization**: camelCase JSON → snake_case Python
- **Required vs optional**: required fields fail fast if missing
- **Env var resolution**: `${VAR}` references are resolved separately before parsing

This matches the pattern in the reference implementation's config schema where all configuration is declared in Pydantic models with field descriptions, constraints, and defaults.

## What Changed From s09

| Component | Before (s08) | After (s09) |
|-----------|-------------|-------------|
| Model selection | Hardcoded in Python | Declared in `config.json` |
| API keys | Hardcoded or `os.getenv()` in code | `${VAR}` references resolved from JSON |
| Channel tokens | In channel class constructors | Per-channel config sections |
| Tool settings | Hardcoded defaults | `tools.web`, `tools.exec` sections |
| Validation | None -- wrong values cause runtime errors | Pydantic validation at startup |
| Single source of truth | No | `config.json` |
| Env separation | Manual | Different JSON files per environment |
| Code changes for config | Required | Not required |
| Defaults | Distributed across files | All in `AgentDefaults()` base model |
| Provider config | None | Per-provider `apiKey`, `apiBase`, `extraHeaders` |
| Schema documentation | None | Pydantic field descriptions and types |

## Try It

```bash
python chapters/10_config.py
```

Suggested prompts:
- `Create a config.json with a different model and temperature -- does the agent pick it up?`
- `Set a channel token via environment variable reference like "botToken": "${MY_BOT_TOKEN}"`
- `What happens if I put an invalid value in the config, like "maxTokens": "not-a-number"?`

---

**Design Note:** In the reference implementation, the full config schema defines `Config`, `ChannelsConfig`, `ProvidersConfig` (20+ providers), `AgentDefaults`, `ToolsConfig` (web, exec, MCP, image generation, my tool), `ApiConfig`, `GatewayConfig`, `DreamConfig`, and more. The loader handles JSON loading, migration from old formats, env var resolution, and SSRF whitelist wiring. All runtime code reads from the validated `Config` object — no `os.getenv()` calls scattered through business logic.