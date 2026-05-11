# s08: Channel System


> **"The agent doesn't know or care if you're on CLI, Telegram, or Discord."**
> Harness layer: Transport

## Problem

In s08, we have session persistence, but the agent still only speaks to one interface at a time. To add Telegram support, you must write Telegram-specific polling logic. To add Discord, you need a WebSocket gateway. Each platform has its own API, authentication, and message format -- and none of that should leak into the agent loop.

If the agent loop has to understand `update.message.text` (Telegram) vs `message.content` (Discord), every channel becomes a special case. Adding the 10th channel means modifying 10 paths in the core code.

## Solution

Define a **`BaseChannel` interface** that every platform implements. Each channel is a self-contained module that:
- Connects to its platform (polling, webhook, WebSocket -- it doesn't matter)
- Translates platform messages into `InboundMessage` dataclasses
- Publishes them to the message bus
- Implements `send()` to deliver `OutboundMessage` back to the platform

The `ChannelManager` discovers, starts, and coordinates all channels. The agent loop never imports Telegram or Discord.

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

Channel registration flow:

  ChannelManager.__init__()
    -> discover_all()          # scans pkgutil + entry_points
    -> for each channel config:
         if enabled:
           channel = ChannelClass(config, bus)
           self.channels[name] = channel

  ChannelManager.start_all()
    -> for each channel:
         asyncio.create_task(channel.start())
```

Each channel is a plugin. Adding a new platform means dropping a new file into `channels/` that implements four methods: `start()`, `stop()`, `send()`, and `login()`.

## How It Works

1. **BaseChannel** defines the abstract contract every channel must fulfill.

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
        """Connect to the platform and begin listening."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect and clean up."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Deliver a response to the platform."""
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

2. **A Telegram channel** implements the interface with `python-telegram-bot`.

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
        # Poll forever
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

3. **ChannelManager** discovers enabled channels from config and orchestrates lifecycle.

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

4. **Channel discovery** uses `pkgutil` scan + entry-point plugins -- no manual registration.

```python
def discover_all() -> dict[str, type[BaseChannel]]:
    """Discover channel implementations via pkgutil + entry_points."""
    channels = {}

    # Scan the openclaw.channels package
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

    # Also load from entry_points group "openclaw.channels"
    channels.update(load_entry_point_channels())
    return channels
```

5. **Outbound routing** is pure dispatch -- the `ChannelManager` reads `msg.channel` and routes to the right `send()` implementation. The agent never knows or cares which platform it's talking to.

## What Changed From s08

| Component | Before (s07) | After (s08) |
|-----------|-------------|-------------|
| Output targets | CLI only | Any platform implementing `BaseChannel` |
| Platform coupling | None (CLI only) | Zero -- agent has no platform imports |
| Channel lifecycle | N/A | `start()` / `stop()` with asyncio tasks |
| Message routing | Direct publish | ChannelManager dispatches by `msg.channel` |
| Permissions | None | `is_allowed()` with `allow_from` whitelist |
| Channel discovery | N/A | Auto-discovery via `pkgutil` + entry_points |
| Retry policy | N/A | Exponential backoff per channel |
| Streaming support | N/A | Optional `send_delta()` for real-time output |
| Multi-channel | N/A | Telegram, Discord, Slack, CLI, and more simultaneously |

## Try It

```bash
python chapters/09_channels.py
```

Suggested prompts:
- `Create a mock "sms" channel that implements BaseChannel -- what methods do you need?`
- `What happens when two channels receive messages at the exact same time?`
- `How does the ChannelManager route responses back to the right platform?`

---

**Design Note:** In the reference implementation, channels support 15+ platforms including Telegram, Discord, Slack, WhatsApp, Matrix, Feishu, DingTalk, WeChat, QQ, Email, WebSocket, and more. Each channel implements `BaseChannel`. The `ChannelManager` handles outbound dispatching with stream delta coalescing, duplicate suppression, retry with exponential backoff, transcription provider wiring, and restart notification delivery.