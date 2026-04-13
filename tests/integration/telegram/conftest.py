"""Fixtures for real-network Telegram integration tests.

This conftest overrides two function-scoped autouse fixtures from the root
``tests/conftest.py`` so that session-scoped runner/client fixtures can live
for the duration of the test run instead of being torn down between tests:

* ``_isolate_hermes_home`` is replaced by a session-scoped variant that picks
  a single HERMES_HOME for the whole integration run.
* ``_enforce_test_timeout`` is replaced by a no-op — real-network round-trips
  through Telegram can exceed 30s; individual tests set their own budgets with
  ``asyncio.wait_for``.

These overrides are scoped to ``tests/integration/telegram/`` only and do not
affect other integration tests in the parent directory.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Override root-level autouse fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _integration_hermes_home(tmp_path_factory):
    """Allocate one HERMES_HOME for the whole integration run."""
    fake = tmp_path_factory.mktemp("hermes_integration_home")
    for sub in ("sessions", "cron", "memories", "skills"):
        (fake / sub).mkdir(exist_ok=True)
    prev = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(fake)
    try:
        import hermes_cli.plugins as _plugins_mod  # noqa: WPS433
        _plugins_mod._plugin_manager = None
    except Exception:
        pass
    for var in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_CHAT_NAME",
        "HERMES_GATEWAY_SESSION",
    ):
        os.environ.pop(var, None)
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


def _skip_if_not_configured() -> None:
    """Call from a fixture body to skip the dependent test cleanly."""
    missing = [k for k in TELEGRAM_INTEGRATION_ENV_VARS if not os.getenv(k)]
    if missing:
        pytest.skip(
            "Telegram integration tests require env vars: "
            + ", ".join(missing)
            + ". See tests/integration/README.md."
        )
    try:
        import telethon  # noqa: F401
    except ImportError:
        pytest.skip(
            "telethon not installed; install with: "
            "uv pip install -e '.[telegram-integration]'"
        )
    # pytest-xdist parallelism would have multiple workers spin up their own
    # GatewayRunner against the same bot token — Telegram only allows one
    # polling consumer per token, so the rest would crash with `Conflict:
    # terminated by other getUpdates`.  Fail fast with a clear message.
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id is not None and worker_id not in ("master", "gw0"):
        pytest.skip(
            "Telegram integration tests must run on a single worker. "
            "Re-run with: pytest tests/integration/telegram/ "
            "-v -m telegram_integration -n 0 -rs"
        )


# ---------------------------------------------------------------------------
# Gateway runner (runs on its own background thread + event loop)
# ---------------------------------------------------------------------------
# The runner uses python-telegram-bot, which starts its own polling loop and
# holds the loop object it was created on.  We run it on a dedicated thread so
# it doesn't fight for the pytest-asyncio loop that Telethon uses on the main
# thread — they only communicate through Telegram servers.

class _RunnerHarness:
    def __init__(self) -> None:
        self.runner = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None

    def start(self, token: str) -> None:
        def _thread_main() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.loop = loop
                from gateway.config import GatewayConfig, Platform, PlatformConfig
                from gateway.run import GatewayRunner

                config = GatewayConfig(
                    platforms={
                        Platform.TELEGRAM: PlatformConfig(enabled=True, token=token),
                    }
                )
                self.runner = GatewayRunner(config=config)
                ok = loop.run_until_complete(self.runner.start())
                if not ok:
                    self._error = RuntimeError(
                        "GatewayRunner.start() returned False — no adapter connected"
                    )
                    self._ready.set()
                    return
                self._ready.set()
                loop.run_forever()
            except BaseException as exc:  # noqa: BLE001
                self._error = exc
                self._ready.set()

        self._thread = threading.Thread(
            target=_thread_main,
            name="integration-gateway",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=60):
            raise TimeoutError("GatewayRunner did not become ready within 60s")
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        if self.loop is None or self.runner is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self.runner.stop(), self.loop)
            fut.result(timeout=30)
        except Exception:
            pass
        # Clean shutdown of runners
        try:
            cancel_fut = asyncio.run_coroutine_threadsafe(
                self._cancel_remaining_tasks(), self.loop,
            )
            cancel_fut.result(timeout=10)
        except Exception:
            pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)

    @staticmethod
    async def _cancel_remaining_tasks() -> None:
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            # clean shutdown
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture(scope="session")
def gateway_runner(_integration_hermes_home):
    _skip_if_not_configured()
    # The real-Telegram test suite runs against a dedicated throwaway bot with
    # a known test user; there's no user base to protect, so open access is
    # the right default.  Without this the gateway's pairing flow kicks in and
    # replies with a pairing code instead of the expected command output.
    overrides = {
        "GATEWAY_ALLOW_ALL_USERS": "true",
        # Bump the text-batching window so the batching test has comfortable
        # timing (default 0.6s is tight when sends round-trip through Telegram).
        # Tests that don't care about batching just see slightly delayed
        # dispatch, which doesn't affect their assertions.
        "HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS": "3.0",
    }
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    harness = _RunnerHarness()
    try:
        harness.start(os.environ["TELEGRAM_TEST_BOT_TOKEN"])
        yield harness.runner
    finally:
        harness.stop()
        # Restore the pre-test env: delete keys that weren't set before
        # (saved value is None), put back the original value otherwise.
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

    Declared lazily so collection works even without pytest-asyncio / telethon
    installed — missing dependencies surface as skips inside the fixture body
    rather than collection errors.
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
# BotChat helper
# ---------------------------------------------------------------------------

class BotChat:
    """Per-test convenience wrapper around a Telethon <-> bot conversation.

    Registers a one-shot event handler on the Telethon client that queues every
    inbound message from the test bot, then exposes ``send`` / ``expect_reply``
    / ``send_and_expect`` helpers with timeouts and multi-chunk coalescing.
    """

    def __init__(self, client, bot_entity) -> None:
        self.client = client
        self.bot_entity = bot_entity
        self._queue: asyncio.Queue = asyncio.Queue()
        self._handler = None

    async def __aenter__(self) -> "BotChat":
        from telethon import events

        async def _on_msg(event):
            await self._queue.put(event.message)

        self.client.add_event_handler(
            _on_msg,
            events.NewMessage(from_users=self.bot_entity),
        )
        self._handler = _on_msg
        return self

    async def __aexit__(self, *exc) -> None:
        if self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:
                pass
            self._handler = None

    async def drain(self) -> None:
        """Discard any pending messages so the next expect_reply starts clean."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def send(self, text: str):
        await self.drain()
        return await self.client.send_message(self.bot_entity, text)

    async def expect_reply_messages(self, timeout: float = 20.0, settle: float = 1.5) -> list:
        """Wait for the next reply, then collect any follow-up chunks that
        arrive within *settle* seconds.  Returns the raw Telethon Message
        objects so tests can inspect entities, attachments, etc.
        """
        first = await asyncio.wait_for(self._queue.get(), timeout)
        messages = [first]
        while True:
            try:
                extra = await asyncio.wait_for(self._queue.get(), settle)
                messages.append(extra)
            except asyncio.TimeoutError:
                break
        return messages

    async def expect_reply(self, timeout: float = 20.0, settle: float = 1.5) -> str:
        """Text-only convenience wrapper around expect_reply_messages."""
        messages = await self.expect_reply_messages(timeout=timeout, settle=settle)
        return "\n".join((m.message or "") for m in messages)

    async def send_and_expect(self, text: str, timeout: float = 20.0) -> str:
        await self.send(text)
        return await self.expect_reply(timeout=timeout)

    async def collect_replies(self, window: float = 5.0, settle: float = 1.5) -> list:
        """Collect every reply that arrives within ``window`` seconds total,
        with a quiet-period of ``settle`` seconds determining "done".

        Unlike ``expect_reply_messages`` this does NOT require at least one
        reply. Returns ``[]`` if nothing arrives within the window.  Useful
        for assertions like "exactly N replies" or "no reply at all".
        """
        import time

        deadline = time.monotonic() + window
        messages: list = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                wait = settle if messages else remaining
                msg = await asyncio.wait_for(self._queue.get(), min(wait, remaining))
                messages.append(msg)
            except asyncio.TimeoutError:
                if messages:
                    # We got at least one and then quiet for `settle` -> done.
                    break
                # Otherwise the full window expired with nothing.
                break
        return messages


def _bot_chat_fixture():
    import pytest_asyncio

    @pytest_asyncio.fixture(loop_scope="session")
    async def bot_chat(telethon_client, gateway_runner):
        bot_username = os.environ["TELEGRAM_TEST_BOT_USERNAME"].lstrip("@")
        bot_entity = await telethon_client.get_entity(bot_username)
        async with BotChat(telethon_client, bot_entity) as chat:
            # Best-effort session reset so each test starts with a fresh transcript.
            try:
                await chat.send("/new")
                await chat.expect_reply(timeout=15.0)
            except Exception:
                # If the bot doesn't reply to /new (e.g. first run with no prior
                # session), continue without failing the test.
                pass
            yield chat

    return bot_chat


bot_chat = _bot_chat_fixture()
