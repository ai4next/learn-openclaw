# s06: 消息总线


> **"将发送者与处理者解耦。"**
> 框架层：通信

## 问题

在 s06 中，智能体循环直接从 `stdin` 读取用户输入，并将响应写入 `stdout`。这种 REPL 模式（`input()` / `print()`）将智能体与单一通道紧密耦合。如果你想添加 Telegram、Discord 或 Slack，你必须为每个平台重写智能体循环——更糟糕的是，将特定于平台的逻辑嵌入核心循环。

这种耦合意味着智能体无法同时服务多个用户，无法在同一会话中接收来自不同平台的消息，也无法在不修改核心代码的情况下进行扩展。输入源和处理逻辑被捆绑在一起。

## 解决方案

引入一个**异步消息总线**——一对 `asyncio.Queue` 实例，将消息生产者（通道）与消费者（智能体循环）解耦。通道将入站消息发布到一个队列；智能体消费它们、处理它们，并将响应发布到出站队列。通道管理器获取出站消息并将其分发回正确的平台。

```
       +-----------+       +-----------+       +-----------+
       | 通道 A    | ----> |  入站     | ----> | 智能体    |
       | (Telegram)|       |  队列     |       | 循环      |
       +-----------+       +-----------+       +-----------+
                                                   |
       +-----------+       +-----------+           |
       | 通道 B    | ----> |  出站     | <---------+
       | (Discord) |       |  队列     |
       +-----------+       +-----------+
              |
       +-----------+
       | 通道      |
       | 管理器    |---- 分发到正确的通道
       +-----------+

数据流：
  通道 -> publish_inbound() -> [入站队列] -> consume_inbound() -> 智能体
  智能体 -> publish_outbound() -> [出站队列] -> consume_outbound() -> 通道管理器 -> Channel.send()
```

总线不关心平台或消息格式。它仅仅是两个队列，带有类型化的数据类在其中流动。

## 工作原理

1. **类型化事件数据类**定义了通道与智能体之间的契约。

```python
@dataclass
class InboundMessage:
    channel: str       # telegram, discord, slack, cli
    sender_id: str     # 用户标识符
    chat_id: str       # 聊天/频道标识符
    content: str       # 消息文本
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    session_key_override: str | None = None

    @property
    def session_key(self) -> str:
        return self.session_key_override or f"{self.channel}:{self.chat_id}"

@dataclass
class OutboundMessage:
    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    buttons: list[list[str]] = field(default_factory=list)
```

2. **MessageBus** 封装了两个 `asyncio.Queue` 实例，并提供发布/消费方法。

```python
class MessageBus:
    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        return await self.outbound.get()
```

3. **通道基类**使用总线将消息从平台转发过来。

```python
class BaseChannel(ABC):
    def __init__(self, config: Any, bus: MessageBus):
        self.config = config
        self.bus = bus
        self._running = False

    async def _handle_message(self, sender_id, chat_id, content,
                              media=None, metadata=None, session_key=None):
        if not self.is_allowed(sender_id):
            return  # 权限检查
        msg = InboundMessage(
            channel=self.name, sender_id=str(sender_id),
            chat_id=str(chat_id), content=content,
            media=media or [], metadata=metadata or {},
            session_key_override=session_key,
        )
        await self.bus.publish_inbound(msg)

    @abstractmethod
    async def start(self): ...
    @abstractmethod
    async def stop(self): ...
    @abstractmethod
    async def send(self, msg: OutboundMessage): ...
```

4. **ChannelManager** 拉取出站消息并将其分发到正确的通道。

```python
class ChannelManager:
    def __init__(self, config, bus, session_manager=None):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None

    async def start_all(self):
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        for name, channel in self.channels.items():
            asyncio.create_task(channel.start())

    async def _dispatch_outbound(self):
        while True:
            msg = await self.bus.consume_outbound()
            channel = self.channels.get(msg.channel)
            if channel:
                await self._send_with_retry(channel, msg)
```

5. **AgentLoop** 从总线读取消息，而非从 stdin。

```python
class AgentLoop:
    async def run(self):
        while self._running:
            # 非阻塞获取，超时1秒
            msg = await asyncio.wait_for(
                self.bus.consume_inbound(), timeout=1.0
            )
            response = await self._process_message(msg)
            if response:
                await self.bus.publish_outbound(response)
```

智能体循环不再关心消息来自何处。它接收 `InboundMessage` 对象并返回 `OutboundMessage` 对象。总线是唯一的集成点。

## 从 s06 以来的变化

| 组件 | 之前 (s05) | 之后 (s06) |
|-----------|-------------|-------------|
| 输入源 | `input()` / stdin | `asyncio.Queue`（消息总线） |
| 输出目标 | `print()` / stdout | `asyncio.Queue`（消息总线） |
| 通道耦合 | 紧密——智能体感知 I/O | 无——智能体只知道 `InboundMessage` / `OutboundMessage` |
| 并发性 | 单线程，顺序执行 | 异步——多个通道可同时向总线投递消息 |
| 平台支持 | 仅 CLI | 任意——Telegram、Discord、CLI 都使用同一总线 |
| 智能体循环签名 | `def agent_loop(messages)` | `async def run()` -> 从总线消费 |
| 出站路由 | 直接 print | ChannelManager 按 `msg.channel` 分发 |
| 消息契约 | 原始字符串 | 带元数据的类型化数据类 |
| 权限模型 | 无 | 带 `allow_from` 白名单的 `BaseChannel.is_allowed()` |

## 尝试运行

```bash
python chapters/07_message_bus.py
```

建议的提示词：
- `通过 "cli" 通道发送一条消息，观察它如何通过总线路由`
- `模拟两个用户同时发送消息——总线是否会序列化它们？`
- `查看 InboundMessage.session_key 属性——它如何从 channel 和 chat_id 派生 session_key？`

---

**设计说明：** 在参考实现中，`MessageBus` 是异步架构的核心。每个通道（Telegram、Discord、Slack、WhatsApp 等）发布 `InboundMessage` 事件。`AgentLoop` 消费这些事件，而 `ChannelManager` 通过重试逻辑、流合并和重复抑制来分发出站响应。总线本身保持精简——两个队列，四个方法，零平台感知。