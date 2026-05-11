# s08: 渠道系统


> **"Agent 不知道也不关心你是在 CLI、Telegram 还是 Discord 上。"**
> 抽象层：Transport

## 问题

在 s07 中，我们实现了会话持久化，但 agent 仍然一次只能与一个接口通信。要添加 Telegram 支持，你必须编写 Telegram 特定的轮询逻辑。要添加 Discord，你需要一个 WebSocket 网关。每个平台都有自己的 API、认证和消息格式——这些都不应该泄露到 agent 循环中。

如果 agent 循环必须理解 `update.message.text`（Telegram）与 `message.content`（Discord）的区别，那么每个渠道都变成了一个特例。添加第 10 个渠道意味着修改核心代码中的 10 条路径。

## 解决方案

定义一个 **`BaseChannel` 接口**，每个平台都实现该接口。每个渠道都是一个自包含的模块，它：

- 连接到其平台（轮询、webhook、WebSocket——方式不限）
- 将平台消息转换为 `InboundMessage` 数据类
- 将它们发布到消息总线
- 实现 `send()` 以将 `OutboundMessage` 传递回平台

`ChannelManager` 负责发现、启动和协调所有渠道。Agent 循环从不导入 Telegram 或 Discord。

```
               ChannelManager
               +------------+
               | discover() |
               | start_all()|-----> Channel A (Telegram)
               | stop_all() |           - polling loop
               +------------+           - _handle_message()
                      |                 - send()
               +------------+
               | MessageBus |
               | inbound    |<---------- Channel A publishes
               | outbound   |----------> ChannelManager dispatches
               +------------+
                      |
               +------------+
               | AgentLoop  |
               | run()      |-----> consume_inbound()
               +------------+        publish_outbound()

渠道注册流程：

  ChannelManager.__init__()
    -> discover_all()          # 扫描 pkgutil + entry_points
    -> for each channel config:
         if enabled:
           channel = ChannelClass(config, bus)
           self.channels[name] = channel

  ChannelManager.start_all()
    -> for each channel:
         asyncio.create_task(channel.start())
```

每个渠道都是一个插件。添加一个新平台意味着在 `channels/` 目录中放入一个实现了四个方法的新文件：`start()`、`stop()`、`send()` 和 `login()`。

## 工作原理

1. **BaseChannel** 定义了每个渠道必须履行的抽象契约。

```python
class BaseChannel(ABC):
    name: str = "base"
    display_name: str = "Base"

    def __init__(self, config: Any, bus: MessageBus):
        self.config = config
        self.bus = bus
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """连接到平台并开始监听。"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """断开连接并清理资源。"""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """将响应发送到平台。"""
        pass

    async def _handle_message(self, sender_id, chat_id, content,
                              media=None, metadata=None, session_key=None):
        if not self.is_allowed(sender_id):
            return
        msg = InboundMessage(
            channel=self.name, sender_id=str(sender_id),
            chat_id=str(chat_id), content=content,
            media=media or [], metadata=metadata or {},
            session_key_override=session_key,
        )
        await self.bus.publish_inbound(msg)

    def is_allowed(self, sender_id: str) -> bool:
        allow = self.config.get("allow_from", [])
        if not allow:
            return False
        if "*" in allow:
            return True
        return str(sender_id) in allow
```

2. **一个 Telegram 渠道** 使用 `python-telegram-bot` 实现了该接口。

```python
class TelegramChannel(BaseChannel):
    name = "telegram"
    display_name = "Telegram"

    async def start(self):
        self._running = True
        self.app = Application.builder().token(
            self.config["bot_token"]
        ).build()
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self._on_message
        ))
        await self.app.initialize()
        await self.app.start()
        # 永久轮询
        while self._running:
            await asyncio.sleep(1)

    async def send(self, msg: OutboundMessage):
        await self.app.bot.send_message(
            chat_id=msg.chat_id,
            text=msg.content,
        )

    async def _on_message(self, update, context):
        await self._handle_message(
            sender_id=str(update.effective_user.id),
            chat_id=str(update.effective_chat.id),
            content=update.message.text,
        )
```

3. **ChannelManager** 从配置中发现已启用的渠道并编排生命周期。

```python
class ChannelManager:
    def __init__(self, config, bus, session_manager=None):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._init_channels()
        self._dispatch_task = None

    def _init_channels(self):
        from openclaw.channels.registry import discover_all
        for name, cls in discover_all().items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = section.get("enabled", False)
            if not enabled:
                continue
            channel = cls(section, self.bus)
            self.channels[name] = channel

    async def start_all(self):
        self._dispatch_task = asyncio.create_task(
            self._dispatch_outbound()
        )
        for name, channel in self.channels.items():
            asyncio.create_task(channel.start())

    async def _dispatch_outbound(self):
        while True:
            msg = await self.bus.consume_outbound()
            channel = self.channels.get(msg.channel)
            if channel:
                await self._send_with_retry(channel, msg)

    @staticmethod
    async def _send_with_retry(channel, msg, max_attempts=3):
        for attempt in range(max_attempts):
            try:
                await channel.send(msg)
                return
            except Exception as e:
                if attempt == max_attempts - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
```

4. **渠道发现** 使用 `pkgutil` 扫描 + entry-point 插件——无需手动注册。

```python
def discover_all() -> dict[str, type[BaseChannel]]:
    """通过 pkgutil + entry_points 发现渠道实现。"""
    channels = {}

    # 扫描 openclaw.channels 包
    import pkgutil
    import openclaw.channels as pkg
    for importer, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if ispkg:
            continue
        module = importer.find_module(modname).load_module(modname)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and
                issubclass(attr, BaseChannel) and
                attr is not BaseChannel):
                channels[attr.name] = attr

    # 同时从 entry_points 组 "openclaw.channels" 加载
    channels.update(load_entry_point_channels())
    return channels
```

5. **出站路由** 是纯粹的分发——`ChannelManager` 读取 `msg.channel` 并路由到对应的 `send()` 实现。Agent 从不知道也不关心它在与哪个平台通信。

## 从 s07 以来的变化

| 组件 | 之前 (s07) | 之后 (s08) |
|-----------|-------------|-------------|
| 输出目标 | 仅 CLI | 任何实现了 `BaseChannel` 的平台 |
| 平台耦合 | 无（仅 CLI） | 零——agent 没有平台导入 |
| 渠道生命周期 | N/A | `start()` / `stop()` 配合 asyncio 任务 |
| 消息路由 | 直接发布 | ChannelManager 根据 `msg.channel` 分发 |
| 权限 | 无 | `is_allowed()` 配合 `allow_from` 白名单 |
| 渠道发现 | N/A | 通过 `pkgutil` + entry_points 自动发现 |
| 重试策略 | N/A | 每个渠道的指数退避 |
| 流式支持 | N/A | 可选的 `send_delta()` 实现实时输出 |
| 多渠道 | N/A | 同时支持 Telegram、Discord、Slack、CLI 等 |

## 尝试运行

```bash
python chapters/09_channels.py
```

建议的提示词：

- `创建一个模拟的 "sms" 渠道来实现 BaseChannel——你需要哪些方法？`
- `当两个渠道在同一时刻接收到消息时会发生什么？`
- `ChannelManager 如何将响应路由回正确的平台？`

---

**设计说明：** 在参考实现中，渠道支持 15+ 个平台，包括 Telegram、Discord、Slack、WhatsApp、Matrix、飞书、钉钉、微信、QQ、邮件、WebSocket 等。每个渠道都实现了 `BaseChannel`。`ChannelManager` 处理出站分发，包括流式增量合并、重复抑制、指数退避重试、转录服务接入和重启通知投递。