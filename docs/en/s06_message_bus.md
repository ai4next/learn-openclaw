# s06: Message Bus


> **"Decouple who sends from who processes."**
> Harness layer: Communication

## Problem

Through s06, the agent loop reads user input directly from `stdin` and writes responses to `stdout`. The REPL pattern (`input()` / `print()`) tightly couples the agent to a single channel. If you want to add Telegram, Discord, or Slack, you have to rewrite the agent loop for each platform -- or worse, thread platform-specific logic through the core loop.

This coupling means the agent cannot serve multiple users simultaneously, cannot receive messages from different platforms in the same session, and cannot be extended without modifying core code. The input source and the processing logic are fused together.

## Solution

Introduce an **async message bus** -- a pair of `asyncio.Queue` instances that decouple message producers (channels) from the consumer (agent loop). Channels publish inbound messages to one queue; the agent consumes them, processes them, and publishes responses to the outbound queue. The channel manager picks up outbound messages and delivers them back to the right platform.

```
       +-----------+       +-----------+       +-----------+
       | Channel A | ----> |  Inbound  | ----> |  Agent    |
       | (Telegram)|       |  Queue    |       |  Loop     |
       +-----------+       +-----------+       +-----------+
                                                   |
       +-----------+       +-----------+           |
       | Channel B | ----> |  Outbound | <---------+
       | (Discord) |       |  Queue    |
       +-----------+       +-----------+
              |
       +-----------+
       | Channel   |
       | Manager   |---- dispatches to correct channel
       +-----------+

Data flow:
  Channel -> publish_inbound() -> [Inbound Queue] -> consume_inbound() -> Agent
  Agent   -> publish_outbound() -> [Outbound Queue] -> consume_outbound() -> ChannelManager -> Channel.send()
```

The bus has no knowledge of platforms or message formats. It is just two queues with typed dataclasses flowing through them.

## How It Works

1. **Typed event dataclasses** define the contract between channels and the agent.

```python
@dataclass
class InboundMessage:
    channel: str       # telegram, discord, slack, cli
    sender_id: str     # User identifier
    chat_id: str       # Chat/channel identifier
    content: str       # Message text
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

2. **MessageBus** wraps two `asyncio.Queue` instances with publish/consume methods.

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

3. **Channel base class** uses the bus to forward messages from the platform.

```python
class BaseChannel(ABC):
    def __init__(self, config: Any, bus: MessageBus):
        self.config = config
        self.bus = bus
        self._running = False

    async def _handle_message(self, sender_id, chat_id, content,
                              media=None, metadata=None, session_key=None):
        if not self.is_allowed(sender_id):
            return  # permission check
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

4. **ChannelManager** pulls outbound messages and dispatches them to the correct channel.

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

5. **AgentLoop** reads from the bus instead of stdin.

```python
class AgentLoop:
    async def run(self):
        while self._running:
            # Non-blocking drain with 1s timeout
            msg = await asyncio.wait_for(
                self.bus.consume_inbound(), timeout=1.0
            )
            response = await self._process_message(msg)
            if response:
                await self.bus.publish_outbound(response)
```

The agent loop no longer knows or cares where messages come from. It receives `InboundMessage` objects and returns `OutboundMessage` objects. The bus is the only integration point.

## What Changed From s06

| Component | Before (s05) | After (s06) |
|-----------|-------------|-------------|
| Input source | `input()` / stdin | `asyncio.Queue` (message bus) |
| Output target | `print()` / stdout | `asyncio.Queue` (message bus) |
| Channel coupling | Tight -- agent knows about I/O | None -- agent only knows `InboundMessage` / `OutboundMessage` |
| Concurrency | Single-threaded, sequential | Async -- multiple channels can feed the bus |
| Platform support | CLI only | Any -- Telegram, Discord, CLI all use the same bus |
| Agent loop signature | `def agent_loop(messages)` | `async def run()` -> consumes from bus |
| Outbound routing | Direct print | ChannelManager dispatches by `msg.channel` |
| Message contract | Raw strings | Typed dataclasses with metadata |
| Permission model | None | `BaseChannel.is_allowed()` with `allow_from` whitelist |

## Try It

```bash
python chapters/07_message_bus.py
```

Suggested prompts:
- `Send a message through the "cli" channel and watch it route through the bus`
- `Simulate two users sending messages at the same time -- does the bus serialize them?`
- `Look at the InboundMessage.session_key property -- how does it derive the key from channel and chat_id?`

---

**Design Note:** In the reference implementation, the `MessageBus` is the heart of the async architecture. Every channel (Telegram, Discord, Slack, WhatsApp, etc.) publishes `InboundMessage` events. The `AgentLoop` consumes them, and the `ChannelManager` dispatches outbound responses with retry logic, stream coalescing, and duplicate suppression. The bus itself stays minimal — two queues, four methods, zero platform awareness.