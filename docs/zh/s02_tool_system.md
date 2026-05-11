# s02: 工具系统


> **"工具就是 JSON Schema。模型读取它们并通过名称调用它们。"**
>
> Harness 层：工具系统

## 问题

一个只能说话的 agent 并没有太大用处。它需要能够执行 shell 命令、读取文件、编写代码、查询数据库——任何开发者能在终端中做的事情。但如何在不将每个动作硬编码到 agent loop 中的情况下，将这些能力赋予模型呢？

解决方案是**工具抽象**：将每项能力定义为一个具名函数，并附带一个描述其参数的 JSON Schema。模型看到这些 schema 并通过名称调用工具。harness 分发调用、捕获结果，并将其反馈到 loop 中。agent loop 本身并不关心每个工具做什么——它只是将 `tool_use` 块路由到注册表，并将 `tool_result` 块返回给模型。

## 解决方案

```
                     +-----------+
                     |  模型     |
                     |  (LLM)    |
                     +-----------+
                          |
                    tool_use 块
                   (名称 + 输入)
                          |
                          v
                   +-----------+
                   | 工具      |
                   | 注册表    |
                   +-----------+
                    /     |    \
                   v      v     v
           +--------+ +--------+ +--------+
           | Bash   | | Read   | | Write  |
           | 工具   | | 工具   | | 工具   |
           +--------+ +--------+ +--------+
                   |      |     |
                   v      v     v
              tool_result 块
                    |
                    v
              Agent Loop 将结果
              追加到消息中
                    |
                    v
              模型继续生成
```

Agent loop 将工具调用视为数据：一个 `tool_use` 块触发一次分发，结果被打包为一个 `tool_result` 块并追加到对话中。模型随后继续生成——可能会调用更多工具，或者结束这次轮次。

## 工作原理

### 1. 定义 Tool 抽象基类

每个工具都是 `Tool` 的子类，包含四个属性：`name`、`description`、`parameters`（JSON Schema）以及一个 `execute` 方法。

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
        """工具参数的 JSON Schema。"""
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

`to_api_schema()` 方法将工具定义转换为 Anthropic API 所期望的格式。这是你的工具定义与模型的工具使用 API 之间的桥梁。

### 2. 实现具体工具

每个工具实现该抽象基类，提供自己的 schema 和执行逻辑。

```python
class BashTool(Tool):
    @property
    def name(self): return "bash"

    @property
    def description(self): return "执行一条 shell 命令"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要运行的 shell 命令"},
            },
            "required": ["command"],
        }

    def execute(self, command: str) -> str:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        # ... 组装并返回输出
```

s02 中提供了三个工具：

| 工具 | 用途 | 参数 |
|------|------|------|
| `bash` | 执行任意 shell 命令 | `command`（字符串） |
| `read` | 从磁盘读取文件 | `file_path`（字符串） |
| `write` | 写入内容到文件 | `file_path`、`content`（字符串） |

### 3. 在注册表中注册工具

工具存放在 `ToolRegistry` 中——一个简单的基于字典的映射，从工具名称到工具实例。

```python
registry = ToolRegistry()
registry.register(BashTool())
registry.register(ReadTool())
registry.register(WriteTool())
```

注册表提供三个操作：`register()`、`list_schemas()`（用于 API 调用）和 `execute()`（用于分发）。

### 4. 将工具 schema 传递给模型

系统提示词现在内联包含了工具 schema，且 API 调用在 `tools` 参数中也包含了它们：

```python
response = client.messages.create(
    model=MODEL,
    system=SYSTEM_PROMPT + json.dumps(registry.list_schemas(), indent=2),
    messages=messages,
    max_tokens=4096,
    tools=registry.list_schemas(),  # <-- 新增
)
```

### 5. 在 loop 中处理 tool_use

当 `stop_reason == "tool_use"` 时，agent 遍历内容块，分发每个工具调用，并将结果作为 `tool_result` 块追加。然后 loop 继续——模型看到结果并决定下一步做什么。

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

Loop 在 `tool_use` 之后不会中断——模型可能会依次调用多个工具。Loop 仅在 `end_turn` 时中断。

### 6. 工具生命周期钩子（增强）

生产环境中的工具具有三阶段生命周期。基类为每个阶段提供了钩子：

```
before_execute(**kwargs)  →  验证、转换或拒绝
       │
       v
execute(**kwargs)         →  执行实际工作
       │
       v
after_execute(result, **kwargs)  →  后处理或注解
```

**`before_execute`** 首先被调用。它可以修改参数或抛出 `ToolPermissionError` 来拒绝调用：

```python
def before_execute(self, command: str) -> dict:
    if not command.strip():
        raise ToolExecutionError("命令不能为空")
    dangerous = ["rm -rf /", ":(){ :|:& };:"]
    if any(d in command for d in dangerous):
        raise ToolPermissionError(f"模式不被允许")
    return {"command": command}
```

**`after_execute`** 在结果返回后被调用。它可以转换输出：

```python
def after_execute(self, result: str, **kwargs) -> str:
    MAX = 10_000
    return result[:MAX] + "..." if len(result) > MAX else result
```

### 7. 错误分类

工具抛出类型化的异常，以便 harness 生成适合模型理解的错误信息：

| 异常 | 含义 | 模型响应 |
|------|------|----------|
| `ToolExecutionError` | 一般性失败（文件未找到、错误的输入） | 尝试不同方法 |
| `ToolPermissionError` | 硬性策略边界 | 停止——询问用户 |
| `ToolSystemError` | 基础设施故障（崩溃、内存不足） | 重试或升级处理 |

注册表捕获每种类型并格式化不同的错误信息。权限错误包含一条"硬性策略边界"提示，让模型不要试图用变通方法重试：

```python
except ToolPermissionError as e:
    return f"权限被拒绝: {e}\n(这是一个硬性策略边界，不要尝试用替代方法重试。)"
```

### 8. 并发安全标志

工具声明它们是否可以并行运行：

```python
@property
def concurrency_safe(self): return True  # ReadTool、BashTool
```

这是给 harness 的一个提示。在后续的章节（s07+）中，支持并发的工具将被并行分发以加快执行速度。

## 从 s01 以来的变化

| 组件 | 之前 (s01) | 之后 (s02) |
|------|------------|------------|
| 工具处理 | 无——纯文本 agent | `Tool` 抽象基类，包含 JSON Schema；`ToolRegistry` 用于分发 |
| 工具生命周期 | 无 | 三阶段：`before_execute → execute → after_execute` |
| 错误处理 | 未捕获的异常导致 loop 崩溃 | 分类错误：`ToolExecutionError`、`ToolPermissionError`、`ToolSystemError` |
| 并发 | 无 | `concurrency_safe` 属性提示并行分发 |
| LLM API 调用 | 无 `tools` 参数 | 包含 `tools=registry.list_schemas()` |
| stop_reason 处理 | 仅检查 `end_turn` | 同时检查 `end_turn` 和 `tool_use` |
| 工具结果反馈 | 无 | `tool_result` 块追加到消息中 |
| 系统提示词 | 静态文本 | 在系统提示词中包含工具 schema |
| Agent 能力 | 仅限对话 | 可以运行 bash、读取文件、写入文件 |

## 试试看

```bash
python chapters/02_tool_system.py
```

建议的提示词：

- "创建一个名为 hello.txt 的文件，内容为 'Hello, OpenClaw!'"
- "读取 /etc/hostname 的内容并告诉我主机名。"
- "列出当前目录下所有 Python 文件并显示它们的大小。"
- "编写一个打印斐波那契数列的 Python 脚本，然后运行它。"

## 可扩展性

添加一个新工具只需要三件事：

1. 创建一个继承自 `Tool` 的新类
2. 定义 `name`、`description`、`parameters` 和 `execute`
3. 注册它：`registry.register(MyNewTool())`

新工具会自动生成 JSON Schema、自动包含到 API 调用中、并自动获得分发能力。Agent loop 和 harness 的其余部分无需更改。