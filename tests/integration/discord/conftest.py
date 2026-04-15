"""Fixtures for real-network Discord integration tests.

Discord-specific wiring on top of the platform-agnostic scaffolding in
``tests/integration/_shared.py``.

Discord prohibits selfbots, so unlike the Telegram suite (which drives a
test bot from a Telethon user account) this one uses **two real bots**:

* The *gateway-under-test* — a normal Hermes Discord adapter, booted by
  ``RunnerHarness`` on a dedicated background thread.
* The *driver* — a second discord.py ``commands.Bot`` running on the
  pytest-asyncio main loop.  It posts test messages into a dedicated
  test guild channel and captures the gateway bot's replies via an
  ``on_message`` listener.

Both bots must be members of the same test guild and have the channel
configured via ``DISCORD_TEST_CHANNEL_ID``.  See
``tests/integration/README.md`` for the one-time setup.

Like the Telegram conftest, this overrides the root conftest's
function-scoped autouse ``_isolate_hermes_home`` and
``_enforce_test_timeout`` fixtures so session-scoped fixtures persist
for the whole run.  Scope is limited to ``tests/integration/discord/``.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Optional

import pytest

from tests.integration._shared import (
    BotChat,
    RunnerHarness,
    build_skip_if_not_configured,
    make_session_hermes_home,
)


# ---------------------------------------------------------------------------
# Override root-level autouse fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _integration_hermes_home(tmp_path_factory):
    prev = os.environ.get("HERMES_HOME")
    fake = make_session_hermes_home(tmp_path_factory)
    yield fake
    if prev is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = prev


@pytest.fixture(autouse=True)
def _isolate_hermes_home(_integration_hermes_home):
    yield


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Override the parent 30s SIGALRM timeout.

    Real-network round-trips through Discord can exceed 30s; tests set
    their own budgets via ``asyncio.wait_for``.
    """
    if sys.platform == "win32":
        yield
        return
    old = signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(0)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ---------------------------------------------------------------------------
# Discord integration test configuration gating
# ---------------------------------------------------------------------------

DISCORD_INTEGRATION_ENV_VARS = (
    "DISCORD_TEST_GATEWAY_BOT_TOKEN",
    "DISCORD_TEST_DRIVER_BOT_TOKEN",
    "DISCORD_TEST_GUILD_ID",
    "DISCORD_TEST_CHANNEL_ID",
    "DISCORD_TEST_GATEWAY_BOT_USER_ID",
)

_skip_if_not_configured = build_skip_if_not_configured(
    env_vars=DISCORD_INTEGRATION_ENV_VARS,
    install_extra="discord-integration",
    dependency_module="discord",
    suite_label="Discord",
    pytest_path_hint="tests/integration/discord/",
)


# ---------------------------------------------------------------------------
# Gateway runner (runs on its own background thread + event loop)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def gateway_runner(_integration_hermes_home):
    _skip_if_not_configured()
    from gateway.config import Platform, PlatformConfig

    # Throwaway test bot in a private test guild; bypass auth + accept
    # the driver bot's messages. Without these:
    #
    #   GATEWAY_ALLOW_ALL_USERS=true
    #     Bypasses the authorization allowlist so the driver bot doesn't
    #     get routed through the pairing-code flow.
    #
    #   DISCORD_ALLOW_BOTS=all
    #     Default is "none" — the gateway's on_message silently drops
    #     messages whose author.bot is True. Our driver IS a bot, so
    #     without this override every test message is discarded before
    #     reaching _handle_message.  See gateway/platforms/discord.py:579.
    #
    #   DISCORD_REQUIRE_MENTION=false
    #     Default is "true" — in non-DM, non-thread channels the
    #     gateway's _handle_message returns early unless the bot was
    #     @mentioned. Our driver sends plain "/help" with no mention.
    #     See gateway/platforms/discord.py:2383,2395.
    #
    #   DISCORD_IGNORE_NO_MENTION=false
    #     Belt-and-braces — this is a *separate* gate in on_message
    #     that fires when the message has mentions but none targets the
    #     gateway bot (see gateway/platforms/discord.py:611).  Our test
    #     messages have no mentions at all so this gate wouldn't fire
    #     anyway, but setting it makes the configuration explicit.
    #
    #   DISCORD_AUTO_THREAD=false
    #     Default is "true" — the gateway creates a thread under each
    #     inbound message and replies inside the thread. The driver's
    #     on_message listener filters on `message.channel.id == test
    #     channel id`, so replies that land in an auto-created thread
    #     have a *different* channel id and never reach the queue.
    #     See gateway/platforms/discord.py:2412-2419.
    #
    #   HERMES_DISCORD_TEXT_BATCH_DELAY_SECONDS=3.0
    #     The batching test needs a window large enough for three
    #     sequential channel.send round-trips; default 0.6s is too tight.
    overrides = {
        "GATEWAY_ALLOW_ALL_USERS": "true",
        "DISCORD_ALLOW_BOTS": "all",
        "DISCORD_REQUIRE_MENTION": "false",
        "DISCORD_IGNORE_NO_MENTION": "false",
        "DISCORD_AUTO_THREAD": "false",
        "HERMES_DISCORD_TEXT_BATCH_DELAY_SECONDS": "3.0",
    }
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    harness = RunnerHarness()
    try:
        harness.start(platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token=os.environ["DISCORD_TEST_GATEWAY_BOT_TOKEN"],
            ),
        })
        yield harness.runner
    finally:
        harness.stop()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Driver bot (runs on the pytest-asyncio event loop, parallel to how the
# Telethon client runs in the Telegram suite)
# ---------------------------------------------------------------------------

def _driver_bot_fixture():
    """Factory returning the pytest_asyncio driver-bot fixture.

    Lazy so collection doesn't blow up if pytest-asyncio / discord.py
    aren't installed; missing deps surface as skips instead of errors.
    """
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="session", loop_scope="session")
    async def discord_driver_bot():
        _skip_if_not_configured()
        import discord
        from discord.ext import commands

        intents = discord.Intents.default()
        # message_content + guild_messages are PRIVILEGED intents — they
        # must be enabled in the Discord Developer Portal for the driver
        # bot, otherwise the on_message listener will fire with empty
        # content and reply assertions will all fail with empty strings.
        intents.message_content = True
        intents.guild_messages = True

        client = commands.Bot(command_prefix="!", intents=intents)
        ready = asyncio.Event()

        @client.event
        async def on_ready():
            ready.set()

        token = os.environ["DISCORD_TEST_DRIVER_BOT_TOKEN"]
        start_task = asyncio.create_task(client.start(token))
        ready_task = asyncio.create_task(ready.wait())
        try:
            # Race: either on_ready fires, client.start() raises (e.g.
            # PrivilegedIntentsRequired, LoginFailure), or 60s elapses.
            # Without this race, exceptions inside start_task are swallowed
            # and all the user sees is a bare 60s TimeoutError with no
            # clue what actually failed.
            done, _pending = await asyncio.wait(
                {ready_task, start_task},
                timeout=60,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if start_task in done:
                # client.start() returning = connection failure.
                # Normal operation keeps it running forever.
                ready_task.cancel()
                exc = start_task.exception()
                hint = (
                    "Discord driver bot failed to connect. Common causes:\n"
                    "  - DISCORD_TEST_DRIVER_BOT_TOKEN is wrong or revoked.\n"
                    "  - Message Content Intent is not enabled on the driver "
                    "bot in the Discord Developer Portal (Bot tab → Privileged "
                    "Gateway Intents → Message Content Intent toggle).\n"
                    "  - Driver bot has not been invited to the test guild."
                )
                if exc is not None:
                    raise RuntimeError(f"{hint}\n\nUnderlying error: {exc!r}") from exc
                raise RuntimeError(
                    f"{hint}\n\n(client.start() returned without raising)"
                )
            if ready_task not in done:
                start_task.cancel()
                ready_task.cancel()
                raise TimeoutError(
                    "Driver bot did not become ready within 60s and "
                    "client.start() did not raise. Network stall? "
                    "Check that your host can reach gateway.discord.gg."
                )
            yield client
        finally:
            try:
                await client.close()
            except Exception:
                pass
            for t in (ready_task, start_task):
                if not t.done():
                    t.cancel()
                try:
                    await asyncio.wait_for(t, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass

    return discord_driver_bot


discord_driver_bot = _driver_bot_fixture()


# ---------------------------------------------------------------------------
# bot_chat fixture — wires shared BotChat with discord.py callbacks
# ---------------------------------------------------------------------------

def _bot_chat_fixture():
    import pytest_asyncio

    @pytest_asyncio.fixture(loop_scope="session")
    async def bot_chat(discord_driver_bot, gateway_runner):
        channel_id = int(os.environ["DISCORD_TEST_CHANNEL_ID"])
        gateway_user_id = int(os.environ["DISCORD_TEST_GATEWAY_BOT_USER_ID"])
        channel = discord_driver_bot.get_channel(channel_id)
        if channel is None:
            channel = await discord_driver_bot.fetch_channel(channel_id)
        if channel is None:
            pytest.skip(
                f"Driver bot cannot see channel {channel_id}. "
                "Check DISCORD_TEST_CHANNEL_ID and that the driver bot "
                "is a member of the test guild."
            )

        queue: asyncio.Queue = asyncio.Queue()

        async def _on_message(message):
            # Filter to replies from the gateway-under-test in our test
            # channel.  The driver might also see its own sends — bots
            # don't normally receive their own on_message in discord.py,
            # but the author check is a safe belt-and-braces.
            if message.channel.id != channel_id:
                return
            if message.author.id != gateway_user_id:
                return
            await queue.put(message)

        discord_driver_bot.add_listener(_on_message, "on_message")
        chat = BotChat(
            queue=queue,
            send_callback=lambda text: channel.send(text),
            text_extractor=lambda m: m.content or "",
        )
        try:
            # Best-effort session reset so each test starts fresh.
            try:
                await chat.send("/new")
                await chat.expect_reply(timeout=15.0)
            except Exception:
                # No prior session — first run; carry on.
                pass
            yield chat
        finally:
            try:
                discord_driver_bot.remove_listener(_on_message, "on_message")
            except Exception:
                pass

    return bot_chat


bot_chat = _bot_chat_fixture()
