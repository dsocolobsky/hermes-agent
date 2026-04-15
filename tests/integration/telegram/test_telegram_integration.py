"""Real-network Telegram integration tests.

These tests boot a real ``GatewayRunner`` with a real bot token and drive it
from a real Telethon user account over the live Telegram network.  All
assertions target deterministic, built-in command output (no LLM in the loop).

Gated behind the ``telegram_integration`` pytest marker (excluded from the
default run via ``addopts`` in pyproject.toml) and the ``TELEGRAM_TEST_*``
env vars.

Run locally:

    uv pip install -e '.[all,dev,telegram-integration]'
    pytest tests/integration/telegram/ -v -m telegram_integration -n 0

``-n 0`` is important: only one worker can poll the bot at a time.  See
``tests/integration/README.md`` for full setup.
"""
from __future__ import annotations

import re

import pytest

pytestmark = [pytest.mark.telegram_integration]

# ---------------------------------------------------------------------------
# Deterministic command flows that do not involve LLM calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="session")
async def test_smoke_help(bot_chat):
    """The full loop closes: /help round-trips through real Telegram."""
    reply = await bot_chat.send_and_expect("/help", timeout=30.0)
    assert "Hermes Commands" in reply, (
        f"/help reply did not contain expected header. Got: {reply!r}"
    )

@pytest.mark.asyncio(loop_scope="session")
async def test_help_lists_core_commands(bot_chat):
    """/help should list the built-in lifecycle commands."""
    reply = await bot_chat.send_and_expect("/help", timeout=30.0)
    for cmd in ("/help", "/new", "/status"):
        assert cmd in reply, f"/help reply missing {cmd!r}. Got: {reply!r}"


@pytest.mark.asyncio(loop_scope="session")
async def test_status_reports_session(bot_chat):
    """/status should return the gateway status block with a session id."""
    reply = await bot_chat.send_and_expect("/status", timeout=30.0)
    assert "Hermes Gateway Status" in reply, f"missing header. Got: {reply!r}"
    assert "Session ID" in reply, f"missing 'Session ID' label. Got: {reply!r}"
    assert "telegram" in reply.lower(), (
        f"/status should list telegram as a connected platform. Got: {reply!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_commands_paginated(bot_chat):
    """/commands returns page 1 by default; /commands 2 returns page 2."""
    page1 = await bot_chat.send_and_expect("/commands", timeout=30.0)
    assert "Commands" in page1, f"/commands missing header. Got: {page1!r}"
    # Header format: "📚 Commands (N total, page 1/M)" — match loosely.
    m1 = re.search(r"page\s+(\d+)\s*/\s*(\d+)", page1)
    assert m1, f"/commands page header not found. Got: {page1!r}"
    page_num, total_pages = int(m1.group(1)), int(m1.group(2))
    assert page_num == 1, f"default /commands should land on page 1, got {page_num}"

    if total_pages >= 2:
        page2 = await bot_chat.send_and_expect("/commands 2", timeout=30.0)
        m2 = re.search(r"page\s+(\d+)\s*/\s*(\d+)", page2)
        assert m2, f"/commands 2 page header not found. Got: {page2!r}"
        assert int(m2.group(1)) == 2, f"/commands 2 should land on page 2. Got: {page2!r}"
        # Different content between pages 1 and 2.
        assert page1 != page2, "page 1 and page 2 returned identical content"


_SESSION_ID_RE = re.compile(r"Session ID[^A-Za-z0-9_-]*([A-Za-z0-9_-]{6,})")


def _session_id_from_status(reply: str) -> str:
    m = _SESSION_ID_RE.search(reply)
    assert m, f"could not find Session ID in /status reply: {reply!r}"
    return m.group(1)


@pytest.mark.asyncio(loop_scope="session")
async def test_new_resets_session(bot_chat):
    """/new produces a new session_id surfaced via /status."""
    before = await bot_chat.send_and_expect("/status", timeout=30.0)
    sid_before = _session_id_from_status(before)

    reset_reply = await bot_chat.send_and_expect("/new", timeout=30.0)
    assert "Session reset" in reset_reply or "New session" in reset_reply, (
        f"/new did not confirm reset. Got: {reset_reply!r}"
    )

    after = await bot_chat.send_and_expect("/status", timeout=30.0)
    sid_after = _session_id_from_status(after)
    assert sid_before != sid_after, (
        f"/new did not produce a new session id "
        f"(before={sid_before!r}, after={sid_after!r})"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_yolo_toggles(bot_chat):
    """Two consecutive /yolo invocations flip the session bypass on and off."""
    reply_a = await bot_chat.send_and_expect("/yolo", timeout=30.0)
    reply_b = await bot_chat.send_and_expect("/yolo", timeout=30.0)

    def _state(text: str) -> str:
        # Reply form: "YOLO mode ON ..." or "YOLO mode OFF ..."
        m = re.search(r"YOLO mode\s+\**(ON|OFF)\**", text)
        assert m, f"could not parse YOLO state from {text!r}"
        return m.group(1)

    state_a, state_b = _state(reply_a), _state(reply_b)
    assert {state_a, state_b} == {"ON", "OFF"}, (
        f"two /yolo calls should produce one ON and one OFF, got {state_a}/{state_b}"
    )

    # Cleanup: leave the session in OFF state regardless of starting state, so
    # later tests aren't surprised if they ever check yolo.
    if state_b == "ON":
        await bot_chat.send_and_expect("/yolo", timeout=30.0)


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_command_replies_helpfully(bot_chat):
    """An unknown /command should produce an 'Unknown command' notice."""
    reply = await bot_chat.send_and_expect(
        "/notarealcommand_xyz", timeout=30.0,
    )
    assert "Unknown command" in reply, (
        f"unknown command should produce 'Unknown command' notice. Got: {reply!r}"
    )
    assert "/commands" in reply, (
        f"unknown command notice should point at /commands. Got: {reply!r}"
    )

@pytest.mark.asyncio(loop_scope="session")
async def test_profile_command(bot_chat):
    """/profile shows the active profile name and HERMES_HOME path."""
    reply = await bot_chat.send_and_expect("/profile", timeout=30.0)
    assert "Profile" in reply, f"missing 'Profile' label. Got: {reply!r}"
    assert "Home" in reply, f"missing 'Home' label. Got: {reply!r}"
    # The integration HERMES_HOME is a tmp dir created by _integration_hermes_home;
    # its name starts with "hermes_integration_home".
    assert "hermes_integration_home" in reply, (
        f"/profile should report the integration tmp HERMES_HOME, got: {reply!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_title_round_trips_through_status(bot_chat):
    """Setting a session title via /title surfaces in the next /status."""
    title = f"integration-test-{__import__('uuid').uuid4().hex[:8]}"
    set_reply = await bot_chat.send_and_expect(f"/title {title}", timeout=30.0)
    assert "Session title set" in set_reply or title in set_reply, (
        f"/title did not confirm. Got: {set_reply!r}"
    )

    status_reply = await bot_chat.send_and_expect("/status", timeout=30.0)
    assert title in status_reply, (
        f"/status did not surface the title we just set. "
        f"title={title!r}, /status={status_reply!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_usage_with_no_agent_history(bot_chat):
    """/usage with no agent calls reports the deterministic 'no data' fallback.

    The bot_chat fixture runs /new before each test, so any cached agent gets
    evicted and the running-agent map is empty -> /usage hits its fallback
    branch which reports either 'No usage data available' or the
    messages-count summary.
    """
    reply = await bot_chat.send_and_expect("/usage", timeout=30.0)
    assert (
        "No usage data available" in reply
        or "Detailed usage available after the first agent response" in reply
    ), f"/usage fallback message not found. Got: {reply!r}"


@pytest.mark.asyncio(loop_scope="session")
async def test_provider_lists_options(bot_chat):
    """/provider lists at least one provider and identifies the current one."""
    reply = await bot_chat.send_and_expect("/provider", timeout=30.0)
    # At least one of the well-known provider identifiers should appear.
    known_providers = ("openrouter", "openai", "anthropic", "nous")
    assert any(p in reply.lower() for p in known_providers), (
        f"/provider reply did not mention any known provider {known_providers!r}. "
        f"Got: {reply!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_personality_lists_or_reports_none(bot_chat):
    """/personality (no args) either lists personalities or reports none configured."""
    reply = await bot_chat.send_and_expect("/personality", timeout=30.0)
    assert (
        "Available Personalities" in reply
        or "No personalities configured" in reply
    ), f"/personality fell into neither expected branch. Got: {reply!r}"


@pytest.mark.asyncio(loop_scope="session")
async def test_retry_with_no_history(bot_chat):
    """/retry on a fresh session (empty transcript) hits the "no previous" branch.

    The bot_chat fixture runs /new before each test, so the session transcript
    starts empty and _handle_retry_command at gateway/run.py:5040 returns the
    deterministic fallback.
    """
    reply = await bot_chat.send_and_expect("/retry", timeout=30.0)
    assert "No previous message to retry" in reply, (
        f"/retry on empty history should return the no-previous-message "
        f"notice. Got: {reply!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_undo_with_no_history(bot_chat):
    """/undo on a fresh session reports the deterministic "nothing to undo"."""
    reply = await bot_chat.send_and_expect("/undo", timeout=30.0)
    assert "Nothing to undo" in reply, (
        f"/undo on empty history should return 'Nothing to undo'. "
        f"Got: {reply!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_compress_with_insufficient_history(bot_chat):
    """/compress on a fresh session reports the deterministic floor.

    _handle_compress_command at gateway/run.py:6166 requires >= 4 messages
    before it'll try to summarise; an empty transcript hits the early return
    without touching the provider.
    """
    reply = await bot_chat.send_and_expect("/compress", timeout=30.0)
    assert "Not enough conversation to compress" in reply, (
        f"/compress on empty history should report the insufficient-history "
        f"notice. Got: {reply!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_verbose_command_gated_off_by_default(bot_chat):
    """/verbose is config-gated; without the gate it returns the helpful notice.

    The test environment doesn't set ``display.tool_progress_command: true``
    in any config.yaml, so the handler at gateway/run.py:5800 hits its
    not-enabled branch and explains how to turn it on.
    """
    reply = await bot_chat.send_and_expect("/verbose", timeout=30.0)
    assert "not enabled" in reply.lower(), (
        f"/verbose should return the gate-disabled notice. Got: {reply!r}"
    )
    assert "tool_progress_command" in reply, (
        f"/verbose notice should mention the config key. Got: {reply!r}"
    )


# ---------------------------------------------------------------------------
# Authorization flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="session")
async def test_pairing_flow_for_unauthorized_user(bot_chat, monkeypatch):
    """With allow-all disabled, an unrecognized DM triggers the pairing flow.

    The harness normally sets ``GATEWAY_ALLOW_ALL_USERS=true`` so the test user
    is auto-authorized.  Here we temporarily revoke that (and any allowlist
    env vars that might be set in CI) so ``_is_user_authorized`` returns
    False and the DM pairing branch at gateway/run.py:2684 fires.

    The bot should reply with a pairing code and the ``hermes pairing approve``
    hint instead of processing the command.  Auth is rechecked per-message
    via ``os.getenv``, so the runtime flip takes effect immediately on the
    background gateway thread.
    """
    # bot_chat's fixture setup already ran (/new was accepted under the
    # normal allow-all).  Now revoke access for the duration of this test.
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "false")
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "false")
    # Clear any allowlists that might bypass the deny: empty strings are
    # treated as "unset" by the auth check (.strip() -> falsy).
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "")

    reply = await bot_chat.send_and_expect("/help", timeout=30.0)

    # /help output starts with "Hermes Commands" — if we see that, auth
    # wasn't actually revoked and the test is invalid.
    assert "Hermes Commands" not in reply, (
        f"auth revocation didn't take effect — bot still answered /help. "
        f"Got: {reply!r}"
    )
    assert "pairing code" in reply.lower(), (
        f"expected pairing-code prompt for unauthorized user. Got: {reply!r}"
    )
    assert "hermes pairing approve" in reply.lower(), (
        f"pairing reply should mention the approve command. Got: {reply!r}"
    )


# ---------------------------------------------------------------------------
# Stage 2: adapter behaviors, more in-depth tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="session")
async def test_markdown_v2_formatting_arrives_intact(bot_chat):
    """The /status reply must arrive as MarkdownV2-formatted text.

    /status emits ``**Hermes Gateway Status**``, ``**Session ID:** \\`<id>\\```,
    and dates with MarkdownV2-special ``-`` and ``:`` chars.  If the adapter's
    MarkdownV2 escaping were broken, Telegram would reject the message and the
    adapter would either drop the reply or fall back to plaintext
    """
    from telethon.tl.types import (
        MessageEntityBold,
        MessageEntityCode,
    )

    await bot_chat.send("/status")
    messages = await bot_chat.expect_reply_messages(timeout=30.0)
    assert len(messages) >= 1

    # assert text content arrived.
    full_text = "\n".join((m.message or "") for m in messages)
    assert "Hermes Gateway Status" in full_text

    # Assert Telegram took the MarkdownV2 path and it's returning Markdown instead of plain text
    all_entities = []
    for m in messages:
        all_entities.extend(m.entities or [])
    has_bold = any(isinstance(e, MessageEntityBold) for e in all_entities)
    has_code = any(isinstance(e, MessageEntityCode) for e in all_entities)
    assert has_bold or has_code, (
        f"/status reply has no Bold/Code formatting entities, the adapter "
        f"likely fell back to plaintext after a MarkdownV2 parse rejection. "
        f"entities={all_entities!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_rapid_messages_are_batched(bot_chat):
    """Three rapid messages within the batch window should produce ONE reply.

    The Telegram adapter collapses quick sequential messages from the same chat
     into a single MessageEvent inside ``_pending_text_batches``, flushed after
    ``HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS`` of quiet.

    The conftest bumps the batch delay to 3.0s so three sequential
    Telethon ``send_message`` round-trips comfortably fit inside the window.
    """
    await bot_chat.drain()

    # Bypass bot_chat.send (which drains before each call) so the queue keeps
    # everything that arrives during and after the burst.
    for _ in range(3):
        await bot_chat.client.send_message(bot_chat.bot_entity, "/help")

    # Generous collection window: 3s batch wait + adapter dispatch + bot reply
    # round-trip + safety margin.  Settle of 2.5s ensures we don't return early
    # if the adapter sends two quick chunks for a long /help reply.
    replies = await bot_chat.collect_replies(window=15.0, settle=2.5)

    # Group replies into "logical" responses by reply window: chunks of the same
    # /help response arrive close together, so we count *bursts* not raw messages.
    # If batching coalesced our 3 sends into 1 dispatch, we expect 1 burst (which
    # may itself be 1+ chunks if /help is long).
    bursts = _group_into_bursts(replies, gap=2.0)
    assert len(bursts) == 1, (
        f"expected text batching to coalesce 3 rapid /help into 1 reply burst, "
        f"got {len(bursts)} bursts ({len(replies)} total messages). "
        f"first chars of each burst: {[(b[0].message or '')[:60] for b in bursts]!r}"
    )


def _group_into_bursts(messages: list, gap: float) -> list:
    """Group consecutive messages into bursts with gaps < ``gap`` seconds."""
    if not messages:
        return []
    bursts = [[messages[0]]]
    for prev, curr in zip(messages, messages[1:]):
        delta = (curr.date - prev.date).total_seconds()
        if delta <= gap:
            bursts[-1].append(curr)
        else:
            bursts.append([curr])
    return bursts
