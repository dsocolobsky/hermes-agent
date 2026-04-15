"""Fixtures for real-network Telegram integration tests.

Telegram-specific wiring on top of the platform-agnostic scaffolding in
``tests/integration/_shared.py``.

This conftest also overrides two function-scoped autouse fixtures from
the root ``tests/conftest.py`` so that session-scoped runner/client
fixtures can live for the duration of the test run instead of being torn
down between tests:

* ``_isolate_hermes_home`` — replaced with a no-op autouse that defers
  to the session-scoped ``_integration_hermes_home`` below.
* ``_enforce_test_timeout`` — replaced with a no-op; real-network
  round-trips through Telegram can exceed 30s. Tests set their own
  budgets via ``asyncio.wait_for``.

These overrides are scoped to ``tests/integration/telegram/`` only.
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
    """Allocate one HERMES_HOME for the whole integration run."""
    prev = os.environ.get("HERMES_HOME")
    fake = make_session_hermes_home(tmp_path_factory)
    yield fake
    if prev is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = prev


@pytest.fixture(autouse=True)
def _isolate_hermes_home(_integration_hermes_home):
    """Override the function-scoped parent so session-scoped runners persist."""
    yield


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Override the parent 30s SIGALRM timeout.

    Real-network tests set their own timeouts via ``asyncio.wait_for``.
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
# Telegram integration test configuration gating
# ---------------------------------------------------------------------------

TELEGRAM_INTEGRATION_ENV_VARS = (
    "TELEGRAM_TEST_BOT_TOKEN",
    "TELEGRAM_TEST_API_ID",
    "TELEGRAM_TEST_API_HASH",
    "TELEGRAM_TEST_SESSION_STRING",
    "TELEGRAM_TEST_BOT_USERNAME",
)

_skip_if_not_configured = build_skip_if_not_configured(
    env_vars=TELEGRAM_INTEGRATION_ENV_VARS,
    install_extra="telegram-integration",
    dependency_module="telethon",
    suite_label="Telegram",
    pytest_path_hint="tests/integration/telegram/",
)


# ---------------------------------------------------------------------------
# Gateway runner (runs on its own background thread + event loop)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def gateway_runner(_integration_hermes_home):
    _skip_if_not_configured()
    from gateway.config import Platform, PlatformConfig

    # The real-Telegram test suite runs against a dedicated throwaway bot
    # with a known test user; there's no user base to protect, so open
    # access is the right default.  Without this the gateway's pairing
    # flow kicks in and replies with a pairing code instead of the
    # expected command output.
    overrides = {
        "GATEWAY_ALLOW_ALL_USERS": "true",
        # Bump the text-batching window so the batching test has
        # comfortable timing (default 0.6s is tight when sends round-trip
        # through Telegram).  Tests that don't care about batching just
        # see slightly delayed dispatch, which doesn't affect their
        # assertions.
        "HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS": "3.0",
    }
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    harness = RunnerHarness()
    try:
        harness.start(platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token=os.environ["TELEGRAM_TEST_BOT_TOKEN"],
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
# Telethon client (runs on the pytest-asyncio event loop)
# ---------------------------------------------------------------------------

def _telethon_fixture():
    """Factory returning the pytest_asyncio fixture for the client.

    Declared lazily so collection works even without pytest-asyncio /
    telethon installed — missing dependencies surface as skips inside
    the fixture body rather than collection errors.
    """
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="session", loop_scope="session")
    async def telethon_client():
        _skip_if_not_configured()
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        api_id = int(os.environ["TELEGRAM_TEST_API_ID"])
        api_hash = os.environ["TELEGRAM_TEST_API_HASH"]
        session = StringSession(os.environ["TELEGRAM_TEST_SESSION_STRING"])
        client = TelegramClient(session, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            pytest.skip(
                "TELEGRAM_TEST_SESSION_STRING is not authorized. "
                "Regenerate with scripts/gen_telethon_session.py."
            )
        try:
            yield client
        finally:
            await client.disconnect()

    return telethon_client


telethon_client = _telethon_fixture()


# ---------------------------------------------------------------------------
# bot_chat fixture — wires shared BotChat with Telethon callbacks
# ---------------------------------------------------------------------------

def _bot_chat_fixture():
    import pytest_asyncio

    @pytest_asyncio.fixture(loop_scope="session")
    async def bot_chat(telethon_client, gateway_runner):
        from telethon import events

        bot_username = os.environ["TELEGRAM_TEST_BOT_USERNAME"].lstrip("@")
        bot_entity = await telethon_client.get_entity(bot_username)
        queue: asyncio.Queue = asyncio.Queue()

        async def _on_msg(event):
            await queue.put(event.message)

        telethon_client.add_event_handler(
            _on_msg,
            events.NewMessage(from_users=bot_entity),
        )
        chat = BotChat(
            queue=queue,
            send_callback=lambda text: telethon_client.send_message(bot_entity, text),
            text_extractor=lambda m: m.message or "",
        )
        try:
            # Best-effort session reset so each test starts with a fresh transcript.
            try:
                await chat.send("/new")
                await chat.expect_reply(timeout=15.0)
            except Exception:
                # If the bot doesn't reply to /new (e.g. first run with no
                # prior session), continue without failing the test.
                pass
            yield chat
        finally:
            try:
                telethon_client.remove_event_handler(_on_msg)
            except Exception:
                pass

    return bot_chat


bot_chat = _bot_chat_fixture()
