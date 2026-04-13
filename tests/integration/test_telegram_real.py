"""Real-network Telegram integration tests — Stage 1: command flows.

These tests boot a real ``GatewayRunner`` with a real bot token and drive it
from a real Telethon user account over the live Telegram network.  All
assertions target deterministic, built-in command output (no LLM in the loop).

Gated behind the ``real_telegram`` pytest marker (excluded from the default
run via ``addopts`` in pyproject.toml) and the ``TELEGRAM_TEST_*`` env vars.

Run locally:

    uv pip install -e '.[all,dev,real-tests]'
    pytest tests/integration/test_telegram_real.py -v -m real_telegram -n 0

``-n 0`` is important: only one worker can poll the bot at a time.  See
``tests/integration/README.md`` for full setup.
"""
from __future__ import annotations

import re

import pytest

pytestmark = [pytest.mark.real_telegram]


# ---------------------------------------------------------------------------
# Stage 0: smoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="session")
async def test_smoke_help(bot_chat):
    """The full loop closes: /help round-trips through real Telegram."""
    reply = await bot_chat.send_and_expect("/help", timeout=30.0)
    assert "Hermes Commands" in reply, (
        f"/help reply did not contain expected header. Got: {reply!r}"
    )


# ---------------------------------------------------------------------------
# Stage 1: deterministic command flows
# ---------------------------------------------------------------------------

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
