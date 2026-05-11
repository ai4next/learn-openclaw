# s09: 配置管理


> **"配置是数据，不是代码。"**
> Harness 层：声明式配置

## 问题

在 s09 之前，每一个参数都是硬编码的：模型名称、API 密钥、频道令牌、工具设置、工作空间路径。要将模型从 Claude 改为 GPT-4o，你需要修改 Python 文件。要添加一个 Telegram 机器人令牌，你需要修改频道类。配置存在于源代码中，这意味着：

- 每次配置变更都需要修改代码并重新部署
- 不同的环境（开发、预发布、生产）无法共享同一份二进制文件
- 机密信息（API 密钥、令牌）与应用程序逻辑混杂在一起
- 没有一个唯一的真实来源来描述 agent 被配置成什么样子

## 解决方案

将所有配置移到一个 **JSON 文件**中（例如 `config.json`），在启动时加载并通过 **Pydantic 模式**进行验证。该模式使用类型、默认值和文档定义了每一个可调参数。应用程序代码从经过验证的 `Config` 对象中读取，而绝不从原始环境变量中读取。

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

加载流程：

  config.json  -->  load_config()  -->  Config (Pydantic)  -->  AgentLoop.from_config()
       |                 |                      |                        |
  config.json       验证 JSON              解析环境变量             创建实际对象
  config.json       模式结构
```

该配置具有以下特性：
- **声明式** -- 一个 JSON 文件描述 agent 应该是什么样子
- **已验证** -- Pydantic 在启动时捕获拼写错误、类型错误和缺失字段
- **可扩展** -- 频道配置使用 `extra="allow"`，以便插件添加自己的配置段
- **环境感知** -- `${VAR}` 引用从环境变量中解析（机密信息不留在文件中）

## 工作原理

1. **Pydantic 模式**使用类型、默认值和验证定义每一个配置选项。

```python
class AgentDefaults(Base):
    workspace: str = "~/.openclaw/workspace"
    model: str = "anthropic/claude-opus-4-5"
    max_tokens: int = 8192
    temperature: float = 0.1
    context_window_tokens: int = 65_536
    timezone: str = "UTC"
    unified_session: bool = False
    session_ttl_minutes: int = 0  # 0 = 禁用

class ChannelsConfig(Base):
    model_config = ConfigDict(extra="allow")  # 插件添加自己的键
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

2. **配置加载器**读取 JSON 文件并根据模式进行验证。

```python
def load_config(config_path: Path | None = None) -> Config:
    path = config_path or get_config_path()  # 例如 ~/.openclaw/config.json
    config = Config()  # 从默认值开始

    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = _migrate_config(data)  # 处理旧格式
        config = Config.model_validate(data)  # Pydantic 验证

    _apply_ssrf_whitelist(config)  # 加载后设置
    return config
```

3. **环境变量解析**允许在 JSON 中使用 `${VAR}` 引用。

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
    # ... 递归处理字典、列表

def _env_replace(match):
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"配置中引用的环境变量 '{name}' 未设置"
        )
    return value
```

这意味着 JSON 文件中的 `"botToken": "${TELEGRAM_BOT_TOKEN}"` 会从环境中加载令牌——从而将机密信息排除在版本控制之外。

4. **配置驱动的 agent 构造** -- `AgentLoop.from_config()` 从配置对象构建一切。

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

5. **camelCase 别名**让 JSON 使用 JavaScript 惯例，而 Python 代码使用 snake_case。

```python
class Base(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
```

因此 JSON 中的 `{"maxTokens": 8192}` 会透明地映射到 Python 代码中的 `max_tokens`。

## 配置验证

Pydantic 模型在加载时提供验证。这可以在配置错误导致运行时故障之前将其捕获：

```python
class AgentConfig(BaseModel):
    model: str = Field(default="claude-sonnet-4-6", min_length=1)
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

# 加载时立即验证：
try:
    config = AppConfig(**loaded_data)
except ValidationError as e:
    print(f"配置错误：{e}")
    sys.exit(1)
```

关键的验证模式：

- **字段约束**：`ge=1, le=200000` 限制令牌值范围
- **类型检查**：Pydantic 自动强制类型转换并拒绝无效类型
- **别名规范化**：camelCase JSON 转为 snake_case Python
- **必选与可选**：必填字段在缺失时快速失败
- **环境变量解析**：`${VAR}` 引用在解析之前单独处理

这与参考实现的配置模式一致，所有配置都在 Pydantic 模型中声明，包含字段描述、约束和默认值。

## 从 s09 以来的变化

| 组件 | 之前 (s08) | 之后 (s09) |
|-----------|-------------|-------------|
| 模型选择 | 在 Python 中硬编码 | 在 `config.json` 中声明 |
| API 密钥 | 在代码中硬编码或使用 `os.getenv()` | 从 JSON 中解析的 `${VAR}` 引用 |
| 频道令牌 | 在频道类构造函数中 | 每个频道的配置段 |
| 工具设置 | 硬编码的默认值 | `tools.web`、`tools.exec` 配置段 |
| 验证 | 无 -- 错误值导致运行时错误 | 启动时进行 Pydantic 验证 |
| 唯一真实来源 | 无 | `config.json` |
| 环境隔离 | 手动 | 每个环境不同的 JSON 文件 |
| 配置变更需改代码 | 需要 | 不需要 |
| 默认值 | 分布在多个文件中 | 全部集中在 `AgentDefaults()` 基础模型中 |
| 提供者配置 | 无 | 每个提供者的 `apiKey`、`apiBase`、`extraHeaders` |
| 模式文档 | 无 | Pydantic 字段描述和类型 |

## 尝试一下

```bash
python chapters/10_config.py
```

建议的提示词：
- `创建一个 config.json，使用不同的模型和温度——agent 会读取到吗？`
- `通过环境变量引用设置频道令牌，例如 "botToken": "${MY_BOT_TOKEN}"`
- `如果在配置中放入一个无效值，例如 "maxTokens": "not-a-number"，会发生什么？`

---

**设计说明：** 在参考实现中，完整的配置模式定义了 `Config`、`ChannelsConfig`、`ProvidersConfig`（20+ 个提供者）、`AgentDefaults`、`ToolsConfig`（web、exec、MCP、图像生成、我的工具）、`ApiConfig`、`GatewayConfig`、`DreamConfig` 等。加载器负责 JSON 加载、旧格式迁移、环境变量解析和 SSRF 白名单配置。所有运行时代码都从经过验证的 `Config` 对象中读取——业务逻辑中不再散布 `os.getenv()` 调用。