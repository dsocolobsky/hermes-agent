"""Shared fixtures for gateway e2e tests (Telegram, Discord, Slack).

These tests exercise the full async message flow:
    adapter.handle_message(event)
        → background task
        → GatewayRunner._handle_message (command dispatch)
        → adapter.send() (captured by mock)

No LLM, no real platform connections.
"""

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

E2E_MESSAGE_SETTLE_DELAY = 0.3  # Small pause to let background message tasks complete.
SETTLE_DELAY = E2E_MESSAGE_SETTLE_DELAY

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource, build_session_key


# Platform library mocks

# Ensure telegram module is available (mock it if not installed)
def _ensure_telegram_mock():
    """Install mock telegram modules so TelegramAdapter can be imported."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return # Real library installed

    telegram_mod = MagicMock()
    telegram_mod.Update = MagicMock()
    telegram_mod.Update.ALL_TYPES = []
    telegram_mod.Bot = MagicMock
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.ext.Application = MagicMock()
    telegram_mod.ext.Application.builder = MagicMock
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.ext.MessageHandler = MagicMock
    telegram_mod.ext.CommandHandler = MagicMock
    telegram_mod.ext.filters = MagicMock()
    telegram_mod.request.HTTPXRequest = MagicMock

    for name in (
        "telegram",
        "telegram.constants",
        "telegram.ext",
        "telegram.ext.filters",
        "telegram.request",
    ):
        sys.modules.setdefault(name, telegram_mod)


# Ensure discord module is available (mock it if not installed)
def _ensure_discord_mock():
    """Install mock discord modules so DiscordAdapter can be imported."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return # Real library installed

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    discord_mod.opus.is_loaded.return_value = True

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)
    sys.modules.setdefault("discord.opus", discord_mod.opus)


def _ensure_slack_mock():
    """Install mock slack modules so SlackAdapter can be imported."""
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return  # Real library installed

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_telegram_mock()
_ensure_discord_mock()
_ensure_slack_mock()

import discord  # noqa: E402 — mocked above
from gateway.platforms.discord import DiscordAdapter   # noqa: E402
from gateway.platforms.telegram import TelegramAdapter  # noqa: E402

import gateway.platforms.slack as _slack_mod  # noqa: E402
_slack_mod.SLACK_AVAILABLE = True
from gateway.platforms.slack import SlackAdapter  # noqa: E402


# Platform-generic factories

def make_source(platform: Platform, chat_id: str = "e2e-chat-1", user_id: str = "e2e-user-1") -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
        user_name="e2e_tester",
        chat_type="dm",
    )


def make_session_entry(platform: Platform, source: SessionSource = None) -> SessionEntry:
    source = source or make_source(platform)
    return SessionEntry(
        session_key=build_session_key(source),
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
        chat_type="dm",
    )


def make_event(platform: Platform, text: str = "/help", chat_id: str = "e2e-chat-1", user_id: str = "e2e-user-1") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=make_source(platform, chat_id, user_id),
        message_id=f"msg-{uuid.uuid4().hex[:8]}",
    )


def make_runner(platform: Platform, session_entry: SessionEntry = None) -> "GatewayRunner":
    """Create a GatewayRunner with mocked internals for e2e testing.

    Skips __init__ to avoid filesystem/network side effects.
    """
    from gateway.run import GatewayRunner

    if session_entry is None:
        session_entry = make_session_entry(platform)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, token="e2e-test-token")}
    )
    runner.adapters = {}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store.reset_session = MagicMock()

    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False

    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *a, **kw: None
    runner._emit_gateway_run_progress = AsyncMock()

    runner.pairing_store = MagicMock()
    runner.pairing_store._is_rate_limited = MagicMock(return_value=False)
    runner.pairing_store.generate_code = MagicMock(return_value="ABC123")

    return runner


def make_adapter(platform: Platform, runner=None):
    """Create a platform adapter wired to *runner*, with send methods mocked."""
    if runner is None:
        runner = make_runner(platform)

    config = PlatformConfig(enabled=True, token="e2e-test-token")

    if platform == Platform.DISCORD:
        with patch.object(DiscordAdapter, "_load_participated_threads", return_value=set()):
            adapter = DiscordAdapter(config)
        platform_key = Platform.DISCORD
    elif platform == Platform.SLACK:
        adapter = SlackAdapter(config)
        platform_key = Platform.SLACK
    else:
        adapter = TelegramAdapter(config)
        platform_key = Platform.TELEGRAM

    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="e2e-resp-1"))
    adapter.send_typing = AsyncMock()

    adapter.set_message_handler(runner._handle_message)
    runner.adapters[platform_key] = adapter

    return adapter


async def send_and_capture(adapter, text: str, platform: Platform, **event_kwargs) -> AsyncMock:
    """Send a message through the full e2e flow and return the send mock."""
    event = make_event(platform, text, **event_kwargs)
    adapter.send.reset_mock()
    await adapter.handle_message(event)
    await asyncio.sleep(E2E_MESSAGE_SETTLE_DELAY)
    return adapter.send


# Parametrized fixtures for platform-generic tests
@pytest.fixture(params=[Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK], ids=["telegram", "discord", "slack"])
def platform(request):
    return request.param


@pytest.fixture()
def source(platform):
    return make_source(platform)


@pytest.fixture()
def session_entry(platform, source):
    return make_session_entry(platform, source)


@pytest.fixture()
def runner(platform, session_entry):
    return make_runner(platform, session_entry)


@pytest.fixture()
def adapter(platform, runner):
    return make_adapter(platform, runner)


# ═══════════════════════════════════════════════════════════════════════════
# Adapter-level test infrastructure
#
# The factories and fixtures below support the adapter-level e2e tests
# (test_discord_adapter.py, test_slack_adapter.py, test_telegram_adapter.py)
# which feed raw platform events through the full pipeline:
#   Raw event → Adapter._handle_message() → handle_message()
#       → GatewayRunner → command dispatch → adapter.send()
# ═══════════════════════════════════════════════════════════════════════════

# Response extraction helpers

def get_response_text(adapter) -> str | None:
    """Extract the response text from adapter.send() call args, or None if not called."""
    if not adapter.send.called:
        return None
    return adapter.send.call_args[1].get("content") or adapter.send.call_args[0][1]


# Discord fakes

BOT_USER_ID = 99999
BOT_USER_NAME = "HermesBot"
AUTHOR_USER_ID = 11111
AUTHOR_USER_NAME = "testuser"
CHANNEL_ID = 22222
THREAD_ID = 33333
GUILD_ID = 44444
MESSAGE_ID_COUNTER = 0


def _next_message_id() -> int:
    global MESSAGE_ID_COUNTER
    MESSAGE_ID_COUNTER += 1
    return 70000 + MESSAGE_ID_COUNTER


def make_fake_bot_user():
    return SimpleNamespace(
        id=BOT_USER_ID,
        name=BOT_USER_NAME,
        display_name=BOT_USER_NAME,
        bot=True,
    )


def make_fake_author(*, bot: bool = False, user_id: int = AUTHOR_USER_ID, name: str = AUTHOR_USER_NAME):
    return SimpleNamespace(id=user_id, name=name, display_name=name, bot=bot)


def make_fake_guild(guild_id: int = GUILD_ID, name: str = "Test Server"):
    return SimpleNamespace(id=guild_id, name=name)


def make_fake_text_channel(channel_id: int = CHANNEL_ID, name: str = "general", guild=None):
    return SimpleNamespace(
        id=channel_id, name=name,
        guild=guild or make_fake_guild(),
        topic=None, type=0,
    )


def make_fake_dm_channel(channel_id: int = 55555):
    ch = MagicMock(spec=[])
    ch.id = channel_id
    ch.name = "DM"
    ch.topic = None
    ch.__class__ = discord.DMChannel
    return ch


def make_fake_thread(thread_id: int = THREAD_ID, name: str = "test-thread", parent=None):
    th = MagicMock(spec=[])
    th.id = thread_id
    th.name = name
    th.parent = parent or make_fake_text_channel()
    th.parent_id = th.parent.id
    th.guild = th.parent.guild
    th.topic = None
    th.type = 11
    th.__class__ = discord.Thread
    return th


def make_fake_attachment(*, filename="photo.png", content_type="image/png",
                         url="https://cdn.example.com/photo.png", size=1024):
    return SimpleNamespace(filename=filename, content_type=content_type, url=url, size=size)


def make_discord_message(
    *, content: str = "hello", author=None, channel=None, mentions=None,
    attachments=None, message_id: int = None, message_type=None,
    reference=None, author_bot: bool = False,
):
    if message_id is None:
        message_id = _next_message_id()
    if author is None:
        author = make_fake_author(bot=author_bot)
    if channel is None:
        channel = make_fake_text_channel()
    if mentions is None:
        mentions = []
    if attachments is None:
        attachments = []
    if message_type is None:
        message_type = getattr(discord, "MessageType", SimpleNamespace()).default

    return SimpleNamespace(
        id=message_id, content=content, author=author, channel=channel,
        mentions=mentions, attachments=attachments, type=message_type,
        reference=reference, created_at=datetime.now(timezone.utc),
        create_thread=AsyncMock(),
    )


# Slack fakes

SLACK_BOT_USER_ID = "U_BOT"
SLACK_USER_ID = "U_USER123"
SLACK_CHANNEL_ID = "C_CHANNEL123"
SLACK_DM_CHANNEL_ID = "D_DM123"
SLACK_TEAM_ID = "T_TEAM123"
SLACK_TS_COUNTER = 0


def _next_slack_ts() -> str:
    global SLACK_TS_COUNTER
    SLACK_TS_COUNTER += 1
    return f"1234567890.{SLACK_TS_COUNTER:06d}"


def make_slack_event(
    *, text: str = "hello", user: str = SLACK_USER_ID,
    channel: str = SLACK_CHANNEL_ID, channel_type: str = "channel",
    ts: str = None, thread_ts: str = None, team: str = SLACK_TEAM_ID,
    bot_id: str = None, subtype: str = None, files: list = None,
) -> dict:
    if ts is None:
        ts = _next_slack_ts()
    event = {
        "type": "message", "text": text, "user": user,
        "channel": channel, "ts": ts, "team": team,
        "channel_type": channel_type,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if bot_id is not None:
        event["bot_id"] = bot_id
    if subtype is not None:
        event["subtype"] = subtype
    if files is not None:
        event["files"] = files
    return event


# Telegram fakes

TELEGRAM_BOT_ID = 12345
TELEGRAM_BOT_USERNAME = "hermesbot"
TELEGRAM_USER_ID = 67890
TELEGRAM_CHAT_ID = 11223

# The adapter uses two different patterns to check chat type:
#   _is_group_chat:       str(chat.type).split(".")[-1].lower() in ("group", "supergroup")
#   _build_message_event: chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
# With mocked ChatType, we need a value that satisfies both.
from telegram.constants import ChatType as _TelegramChatType  # noqa: E402


class _FakeChatType(str):
    """A string that also compares equal to a specific mock object."""

    def __new__(cls, string_val, mock_obj):
        instance = super().__new__(cls, string_val)
        instance._mock_obj = mock_obj
        return instance

    def __eq__(self, other):
        if other is self._mock_obj:
            return True
        return super().__eq__(other)

    def __hash__(self):
        return super().__hash__()


_CHAT_TYPE_MAP = {
    "private": _FakeChatType("private", getattr(_TelegramChatType, "PRIVATE", None)),
    "group": _FakeChatType("group", getattr(_TelegramChatType, "GROUP", None)),
    "supergroup": _FakeChatType("supergroup", getattr(_TelegramChatType, "SUPERGROUP", None)),
    "channel": _FakeChatType("channel", getattr(_TelegramChatType, "CHANNEL", None)),
}


def make_telegram_chat(*, chat_id: int = TELEGRAM_CHAT_ID, chat_type: str = "private", title: str = None):
    return SimpleNamespace(
        id=chat_id,
        type=_CHAT_TYPE_MAP.get(chat_type, chat_type),
        title=title or ("Test Group" if chat_type != "private" else None),
        full_name="Test User",
    )


def make_telegram_user(*, user_id: int = TELEGRAM_USER_ID, first_name: str = "Test", last_name: str = "User"):
    return SimpleNamespace(
        id=user_id, first_name=first_name, last_name=last_name,
        full_name=f"{first_name} {last_name}", username="testuser",
    )


def make_telegram_message(
    *, text: str = "hello", message_id: int = 100, chat=None, from_user=None,
    date=None, reply_to_message=None, entities=None, caption=None,
    caption_entities=None, photo=None, voice=None, audio=None,
    document=None, sticker=None, video=None, message_thread_id=None,
    media_group_id=None, forum_topic_created=None,
):
    return SimpleNamespace(
        text=text, message_id=message_id,
        chat=chat or make_telegram_chat(),
        from_user=from_user or make_telegram_user(),
        date=date or datetime.now(timezone.utc),
        reply_to_message=reply_to_message,
        entities=entities or [], caption=caption,
        caption_entities=caption_entities or [],
        photo=photo, voice=voice, audio=audio, document=document,
        sticker=sticker, video=video,
        message_thread_id=message_thread_id,
        media_group_id=media_group_id,
        forum_topic_created=forum_topic_created,
    )


def make_telegram_update(*, message=None):
    return SimpleNamespace(message=message or make_telegram_message())


# Wired adapter factories — full pipeline through GatewayRunner

def _make_discord_adapter_wired(runner=None):
    if runner is None:
        runner = make_runner(Platform.DISCORD)

    config = PlatformConfig(enabled=True, token="e2e-test-token")
    with patch.object(DiscordAdapter, "_load_participated_threads", return_value=set()):
        adapter = DiscordAdapter(config)

    bot_user = make_fake_bot_user()
    adapter._client = SimpleNamespace(
        user=bot_user,
        get_channel=lambda _id: None,
        fetch_channel=AsyncMock(),
    )

    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="e2e-resp-1"))
    adapter.send_typing = AsyncMock()
    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.DISCORD] = adapter
    return adapter, runner


def _make_slack_adapter_wired(runner=None):
    if runner is None:
        runner = make_runner(Platform.SLACK)

    config = PlatformConfig(enabled=True, token="xoxb-fake-token")
    adapter = SlackAdapter(config)

    adapter._bot_user_id = SLACK_BOT_USER_ID
    adapter._team_bot_user_ids = {SLACK_TEAM_ID: SLACK_BOT_USER_ID}
    adapter._team_clients = {}
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._running = True

    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="e2e-resp-1"))
    adapter.send_typing = AsyncMock()
    adapter._add_reaction = AsyncMock(return_value=True)
    adapter._remove_reaction = AsyncMock(return_value=True)
    adapter._resolve_user_name = AsyncMock(return_value="Test User")

    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.SLACK] = adapter
    return adapter, runner


def _make_telegram_adapter_wired(runner=None):
    if runner is None:
        runner = make_runner(Platform.TELEGRAM)

    config = PlatformConfig(enabled=True, token="telegram-test-token")
    adapter = TelegramAdapter(config)

    adapter._bot = SimpleNamespace(
        id=TELEGRAM_BOT_ID,
        username=TELEGRAM_BOT_USERNAME,
    )

    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="e2e-resp-1"))
    adapter.send_typing = AsyncMock()
    adapter._text_batch_delay_seconds = 0.05
    adapter._media_batch_delay_seconds = 0.05

    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.TELEGRAM] = adapter
    return adapter, runner


# Adapter-level fixtures

@pytest.fixture()
def discord_setup():
    """Returns (adapter, runner) tuple for Discord full-pipeline tests."""
    return _make_discord_adapter_wired()


@pytest.fixture()
def discord_adapter(discord_setup):
    return discord_setup[0]


@pytest.fixture()
def discord_runner(discord_setup):
    return discord_setup[1]


@pytest.fixture()
def bot_user():
    return make_fake_bot_user()


@pytest.fixture()
def slack_setup():
    """Returns (adapter, runner) tuple for Slack full-pipeline tests."""
    return _make_slack_adapter_wired()


@pytest.fixture()
def slack_adapter(slack_setup):
    return slack_setup[0]


@pytest.fixture()
def telegram_setup():
    """Returns (adapter, runner) tuple for Telegram full-pipeline tests."""
    return _make_telegram_adapter_wired()


@pytest.fixture()
def telegram_adapter(telegram_setup):
    return telegram_setup[0]
