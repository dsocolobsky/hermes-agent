"""Full-pipeline integration tests for TelegramAdapter.

Raw telegram.Update → _handle_text_message / _handle_command / _handle_media_message
    → handle_message() → GatewayRunner → command dispatch → adapter.send() response

Tests verify both the adapter's event processing (group rules, mention
stripping, batching) AND that the runner produces correct responses.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tests.e2e.conftest import (
    SETTLE_DELAY,
    TELEGRAM_BOT_ID,
    TELEGRAM_BOT_USERNAME,
    TELEGRAM_CHAT_ID,
    get_response_text,
    make_telegram_chat,
    make_telegram_message,
    make_telegram_update,
    make_telegram_user,
)

pytestmark = pytest.mark.asyncio

TEXT_BATCH_SETTLE = 0.15  # > adapter._text_batch_delay_seconds (0.05)


# ---------------------------------------------------------------------------
# Command handling (commands bypass batching)
# ---------------------------------------------------------------------------

class TestTelegramCommands:
    async def test_help_command(self, telegram_adapter):
        """/help → dispatched, response lists commands."""
        msg = make_telegram_message(text="/help")
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_command(update, None)
        await asyncio.sleep(SETTLE_DELAY)
        response = get_response_text(telegram_adapter)
        assert response is not None
        assert "/new" in response
        assert "/status" in response

    async def test_status_command(self, telegram_adapter):
        """/status → returns session info."""
        msg = make_telegram_message(text="/status")
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_command(update, None)
        await asyncio.sleep(SETTLE_DELAY)
        response = get_response_text(telegram_adapter)
        assert response is not None
        assert "session" in response.lower() or "Session" in response

    async def test_new_command(self, telegram_adapter, telegram_setup):
        """/new → session reset called."""
        _, runner = telegram_setup
        msg = make_telegram_message(text="/new")
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_command(update, None)
        await asyncio.sleep(SETTLE_DELAY)
        response = get_response_text(telegram_adapter)
        assert response is not None
        runner.session_store.reset_session.assert_called_once()

    async def test_command_with_bot_suffix(self, telegram_adapter):
        """/help@hermesbot → bot suffix handled, /help dispatched."""
        msg = make_telegram_message(text=f"/help@{TELEGRAM_BOT_USERNAME}")
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_command(update, None)
        await asyncio.sleep(SETTLE_DELAY)
        response = get_response_text(telegram_adapter)
        assert response is not None
        assert "/new" in response

    async def test_command_in_group_bypasses_mention(self, telegram_adapter, monkeypatch):
        """Commands in groups bypass the mention requirement."""
        monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION", "true")
        telegram_adapter.config.extra["require_mention"] = True
        chat = make_telegram_chat(chat_type="supergroup")
        msg = make_telegram_message(text="/help", chat=chat)
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_command(update, None)
        await asyncio.sleep(SETTLE_DELAY)
        response = get_response_text(telegram_adapter)
        assert response is not None
        assert "/new" in response


# ---------------------------------------------------------------------------
# Text messages (go through batching)
# ---------------------------------------------------------------------------

class TestTelegramTextMessages:
    async def test_dm_text_gets_response(self, telegram_adapter):
        """DM text message → processed and gets a response."""
        msg = make_telegram_message(text="/help")
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_text_message(update, None)
        await asyncio.sleep(TEXT_BATCH_SETTLE)
        response = get_response_text(telegram_adapter)
        assert response is not None

    async def test_text_batching(self, telegram_adapter):
        """Rapid sequential messages → batched into single dispatch, single response."""
        msg1 = make_telegram_message(text="/help", message_id=101)
        msg2 = make_telegram_message(text="more text", message_id=102)
        await telegram_adapter._handle_text_message(make_telegram_update(message=msg1), None)
        await telegram_adapter._handle_text_message(make_telegram_update(message=msg2), None)
        await asyncio.sleep(TEXT_BATCH_SETTLE)

        # Should produce exactly one response (batched into single dispatch)
        assert telegram_adapter.send.await_count == 1


# ---------------------------------------------------------------------------
# Group trigger rules → message dropped
# ---------------------------------------------------------------------------

class TestTelegramGroupRules:
    async def test_group_without_mention_dropped(self, telegram_adapter, monkeypatch):
        """Group messages without mention dropped when require_mention=true."""
        monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION", "true")
        telegram_adapter.config.extra["require_mention"] = True
        chat = make_telegram_chat(chat_type="supergroup")
        msg = make_telegram_message(text="/help", chat=chat)
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_text_message(update, None)
        await asyncio.sleep(TEXT_BATCH_SETTLE)
        assert get_response_text(telegram_adapter) is None

    async def test_group_reply_to_bot_accepted(self, telegram_adapter, monkeypatch):
        """Replying to the bot in a group → accepted, gets response."""
        monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION", "true")
        telegram_adapter.config.extra["require_mention"] = True
        bot_user = make_telegram_user(user_id=TELEGRAM_BOT_ID)
        reply_msg = make_telegram_message(text="bot said something", message_id=50, from_user=bot_user)
        chat = make_telegram_chat(chat_type="supergroup")
        msg = make_telegram_message(text="/help", chat=chat, reply_to_message=reply_msg)
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_text_message(update, None)
        await asyncio.sleep(TEXT_BATCH_SETTLE)
        response = get_response_text(telegram_adapter)
        assert response is not None

    async def test_dm_always_accepted(self, telegram_adapter, monkeypatch):
        """DMs always accepted regardless of mention settings."""
        monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION", "true")
        telegram_adapter.config.extra["require_mention"] = True
        msg = make_telegram_message(text="/help")
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_text_message(update, None)
        await asyncio.sleep(TEXT_BATCH_SETTLE)
        response = get_response_text(telegram_adapter)
        assert response is not None

    async def test_free_response_chat_accepted(self, telegram_adapter, monkeypatch):
        """Messages in free-response chats accepted without mention."""
        monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION", "true")
        telegram_adapter.config.extra["require_mention"] = True
        telegram_adapter.config.extra["free_response_chats"] = [str(TELEGRAM_CHAT_ID)]
        chat = make_telegram_chat(chat_id=TELEGRAM_CHAT_ID, chat_type="supergroup")
        msg = make_telegram_message(text="/help", chat=chat)
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_text_message(update, None)
        await asyncio.sleep(TEXT_BATCH_SETTLE)
        response = get_response_text(telegram_adapter)
        assert response is not None


# ---------------------------------------------------------------------------
# Media handling (mock downloads, verify pipeline continues)
# ---------------------------------------------------------------------------

class TestTelegramMediaHandling:
    async def test_photo_message_gets_response(self, telegram_adapter):
        """Photo message → processed through full pipeline."""
        fake_file = AsyncMock()
        fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x89PNG"))
        fake_file.file_path = "photos/file_1.jpg"
        photo_size = AsyncMock()
        photo_size.get_file = AsyncMock(return_value=fake_file)

        msg = make_telegram_message(text=None, photo=[photo_size], message_id=200)
        update = make_telegram_update(message=msg)

        with patch("gateway.platforms.telegram.cache_image_from_bytes", return_value="/tmp/cached.jpg"):
            await telegram_adapter._handle_media_message(update, None)
            await asyncio.sleep(TEXT_BATCH_SETTLE)

        # Photo goes through the pipeline — it'll reach the runner
        telegram_adapter.send.assert_awaited()

    async def test_voice_message_gets_response(self, telegram_adapter):
        """Voice message → processed through full pipeline."""
        fake_file = AsyncMock()
        fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"OggS"))
        voice = AsyncMock()
        voice.get_file = AsyncMock(return_value=fake_file)

        msg = make_telegram_message(text=None, voice=voice, message_id=201)
        update = make_telegram_update(message=msg)

        with patch("gateway.platforms.telegram.cache_audio_from_bytes", return_value="/tmp/cached.ogg"):
            await telegram_adapter._handle_media_message(update, None)
            await asyncio.sleep(TEXT_BATCH_SETTLE)

        telegram_adapter.send.assert_awaited()

    async def test_group_photo_without_mention_dropped(self, telegram_adapter, monkeypatch):
        """Photo in group without mention → dropped."""
        monkeypatch.setenv("TELEGRAM_REQUIRE_MENTION", "true")
        telegram_adapter.config.extra["require_mention"] = True
        chat = make_telegram_chat(chat_type="supergroup")

        fake_file = AsyncMock()
        fake_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x89PNG"))
        fake_file.file_path = "photos/file_3.jpg"
        photo_size = AsyncMock()
        photo_size.get_file = AsyncMock(return_value=fake_file)

        msg = make_telegram_message(text=None, photo=[photo_size], chat=chat, message_id=203)
        update = make_telegram_update(message=msg)
        await telegram_adapter._handle_media_message(update, None)
        await asyncio.sleep(TEXT_BATCH_SETTLE)
        assert get_response_text(telegram_adapter) is None
