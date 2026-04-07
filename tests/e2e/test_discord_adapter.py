"""Full-pipeline integration tests for DiscordAdapter.

Raw discord.Message → _handle_message() → handle_message() → GatewayRunner
    → command dispatch → adapter.send() response

Tests verify both that the adapter correctly processes the raw event AND that
the runner produces the expected response, covering the full path from
platform event to bot reply.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.e2e.conftest import (
    BOT_USER_ID,
    CHANNEL_ID,
    SETTLE_DELAY,
    get_response_text,
    make_discord_message,
    make_fake_dm_channel,
    make_fake_text_channel,
    make_fake_thread,
)

pytestmark = pytest.mark.asyncio


async def dispatch(adapter, msg):
    await adapter._handle_message(msg)
    await asyncio.sleep(SETTLE_DELAY)


# ---------------------------------------------------------------------------
# Mention stripping → command dispatch
# ---------------------------------------------------------------------------

class TestMentionAndCommandDispatch:
    async def test_mention_stripped_help_command(self, discord_adapter, bot_user):
        """<@BOT> /help → mention stripped, /help dispatched, response lists commands."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response
        assert "/status" in response

    async def test_nickname_mention_stripped_help(self, discord_adapter, bot_user):
        """<@!BOT> /help → nickname mention format also stripped, /help works."""
        msg = make_discord_message(
            content=f"<@!{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_mention_stripped_status_command(self, discord_adapter, bot_user):
        """<@BOT> /status → mention stripped, /status dispatched correctly."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /status",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "session" in response.lower() or "Session" in response

    async def test_mention_stripped_new_command(self, discord_adapter, bot_user, discord_runner):
        """<@BOT> /new → mention stripped, session reset called."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /new",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        discord_runner.session_store.reset_session.assert_called_once()


# ---------------------------------------------------------------------------
# Mention position variants
# ---------------------------------------------------------------------------

class TestMentionPosition:
    async def test_text_before_mention_and_command(self, discord_adapter, bot_user):
        """'Hey <@BOT> /help' → mention stripped, but 'Hey /help' is not a command
        because it doesn't start with /."""
        msg = make_discord_message(
            content=f"Hey <@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        # "Hey  /help" doesn't start with "/" so it's TEXT, not COMMAND.
        # It goes to the agent path (not command dispatch), but the message
        # is still accepted and processed.
        discord_adapter.send.assert_awaited()

    async def test_mention_at_end(self, discord_adapter, bot_user):
        """/help <@BOT> → mention stripped, '/help' detected as command."""
        msg = make_discord_message(
            content=f"/help <@{BOT_USER_ID}>",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_mention_between_words(self, discord_adapter, bot_user):
        """'hello <@BOT> world' → becomes 'hello  world' (interior double space)."""
        msg = make_discord_message(
            content=f"hello <@{BOT_USER_ID}> world",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        # Message is accepted (not dropped), text is "hello  world"
        discord_adapter.send.assert_awaited()

    async def test_only_command_after_mention(self, discord_adapter, bot_user):
        """'<@BOT> /status' → '/status' detected as command, returns session info."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /status",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert "session" in response.lower() or "Session" in response

    async def test_mention_with_surrounding_text_and_command(self, discord_adapter, bot_user):
        """'Hey <@BOT> can you run /help' → not a command (doesn't start with /)."""
        msg = make_discord_message(
            content=f"Hey <@{BOT_USER_ID}> can you run /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        # Text becomes "Hey  can you run /help" — not a command, goes to agent path
        discord_adapter.send.assert_awaited()


# ---------------------------------------------------------------------------
# Message dropped (no response sent)
# ---------------------------------------------------------------------------

class TestMessageDropped:
    async def test_no_mention_in_channel_no_response(self, discord_adapter):
        """Message without @mention in server channel → silently dropped."""
        msg = make_discord_message(content="/help", mentions=[])
        await dispatch(discord_adapter, msg)
        assert get_response_text(discord_adapter) is None

    async def test_require_mention_true_drops(self, discord_adapter, monkeypatch):
        """DISCORD_REQUIRE_MENTION=true → no-mention message dropped."""
        monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
        msg = make_discord_message(content="hello", mentions=[])
        await dispatch(discord_adapter, msg)
        assert get_response_text(discord_adapter) is None

    async def test_new_thread_requires_mention(self, discord_adapter):
        """Thread where bot hasn't participated → no-mention message dropped."""
        thread = make_fake_thread(thread_id=77777)
        msg = make_discord_message(content="/help", channel=thread, mentions=[])
        await dispatch(discord_adapter, msg)
        assert get_response_text(discord_adapter) is None


# ---------------------------------------------------------------------------
# DM handling
# ---------------------------------------------------------------------------

class TestDMHandling:
    async def test_dm_help_no_mention_needed(self, discord_adapter):
        """DMs don't require @mention — /help works directly."""
        dm = make_fake_dm_channel()
        msg = make_discord_message(content="/help", channel=dm, mentions=[])
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_dm_status(self, discord_adapter):
        """DM /status returns session info."""
        dm = make_fake_dm_channel()
        msg = make_discord_message(content="/status", channel=dm)
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "session" in response.lower() or "Session" in response


# ---------------------------------------------------------------------------
# Auto-threading
# ---------------------------------------------------------------------------

class TestAutoThreading:
    async def test_auto_thread_created_and_command_dispatched(self, discord_adapter, bot_user, monkeypatch):
        """@mention in channel → thread created, message still dispatched."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        fake_thread = make_fake_thread(thread_id=90001, name="help")
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        msg.create_thread = AsyncMock(return_value=fake_thread)
        await dispatch(discord_adapter, msg)

        msg.create_thread.assert_awaited_once()
        assert "90001" in discord_adapter._bot_participated_threads
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_auto_thread_disabled_still_dispatches(self, discord_adapter, bot_user, monkeypatch):
        """AUTO_THREAD=false → no thread created, command still works."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        msg.create_thread = AsyncMock()
        await dispatch(discord_adapter, msg)

        msg.create_thread.assert_not_awaited()
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_no_auto_thread_in_existing_thread(self, discord_adapter, bot_user, monkeypatch):
        """Messages already in a thread don't trigger auto-threading."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        thread = make_fake_thread()
        discord_adapter._bot_participated_threads.add(str(thread.id))
        msg = make_discord_message(
            content="/help", channel=thread, mentions=[],
        )
        msg.create_thread = AsyncMock()
        await dispatch(discord_adapter, msg)

        msg.create_thread.assert_not_awaited()
        response = get_response_text(discord_adapter)
        assert response is not None

    async def test_no_auto_thread_in_dm(self, discord_adapter, monkeypatch):
        """DMs don't trigger auto-threading."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        dm = make_fake_dm_channel()
        msg = make_discord_message(content="/help", channel=dm)
        msg.create_thread = AsyncMock()
        await dispatch(discord_adapter, msg)

        msg.create_thread.assert_not_awaited()
        assert get_response_text(discord_adapter) is not None


# ---------------------------------------------------------------------------
# Free-response channels
# ---------------------------------------------------------------------------

class TestFreeResponseChannels:
    async def test_free_channel_no_mention_needed(self, discord_adapter, monkeypatch):
        """Free-response channel → commands work without @mention."""
        channel = make_fake_text_channel(channel_id=CHANNEL_ID)
        monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", str(CHANNEL_ID))
        msg = make_discord_message(content="/help", mentions=[], channel=channel)
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_non_free_channel_still_requires_mention(self, discord_adapter, monkeypatch):
        """Channel NOT in free list → still requires @mention."""
        monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "99998")
        msg = make_discord_message(content="/help", mentions=[])
        await dispatch(discord_adapter, msg)
        assert get_response_text(discord_adapter) is None

    async def test_thread_inherits_free_parent(self, discord_adapter, monkeypatch):
        """Thread in a free-response channel → no mention needed."""
        parent = make_fake_text_channel(channel_id=CHANNEL_ID)
        thread = make_fake_thread(parent=parent)
        monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", str(CHANNEL_ID))
        msg = make_discord_message(content="/status", channel=thread, mentions=[])
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None


# ---------------------------------------------------------------------------
# Thread participation
# ---------------------------------------------------------------------------

class TestThreadParticipation:
    async def test_participated_thread_no_mention(self, discord_adapter):
        """Thread where bot participated → follow-ups work without @mention."""
        thread = make_fake_thread()
        discord_adapter._bot_participated_threads.add(str(thread.id))
        msg = make_discord_message(content="/help", channel=thread, mentions=[])
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_thread_tracked_after_mention(self, discord_adapter, bot_user):
        """After processing a message in a thread, that thread is tracked."""
        thread = make_fake_thread(thread_id=88888)
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            channel=thread, mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        assert "88888" in discord_adapter._bot_participated_threads
        assert get_response_text(discord_adapter) is not None


# ---------------------------------------------------------------------------
# Require-mention config
# ---------------------------------------------------------------------------

class TestRequireMentionConfig:
    async def test_require_mention_false_allows_all(self, discord_adapter, monkeypatch):
        """DISCORD_REQUIRE_MENTION=false → commands work without @mention."""
        monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
        msg = make_discord_message(content="/help", mentions=[])
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response


# ---------------------------------------------------------------------------
# Mention-only (empty text after stripping)
# ---------------------------------------------------------------------------

class TestMentionOnly:
    async def test_mention_only_gets_response(self, discord_adapter, bot_user):
        """@mention with no text → placeholder injected, bot still responds."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}>",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        # The adapter injects placeholder text, which goes to the runner.
        # The runner will try to process it (not a command, so it goes to
        # the agent path which will error in tests, but send still gets called
        # or the message is at least not dropped).
        discord_adapter.send.assert_awaited()
