"""Shared scaffolding for real-network platform integration tests.

Pure-Python module (no pytest fixtures) consumed by the per-platform
conftests under ``tests/integration/<platform>/``.  Keeps the autouse
fixture overrides scoped to platform sub-directories so the rest of the
``tests/integration/`` tree (which uses the parent root-conftest
function-scoped HERMES_HOME isolation) is unaffected.

Exposes:

* ``make_session_hermes_home`` — allocate a single tmp HERMES_HOME for
  the suite, with the same env scrubs as the root conftest's
  per-function fixture.
* ``RunnerHarness`` — runs a ``GatewayRunner`` on a dedicated background
  thread + event loop so it doesn't fight pytest-asyncio's loop.
  Parameterised over the platforms dict, so any adapter can drive it.
* ``build_skip_if_not_configured`` — factory returning a ``_skip(...)``
  callable that handles env-var checks, optional dependency import
  guard, and the pytest-xdist "must run on a single worker" guard.
* ``BotChat`` — platform-agnostic conversation helper. Receives
  inbound messages via an externally-managed queue and sends outbound
  messages via an injected callable. Each platform's ``bot_chat``
  fixture is responsible for handler registration / teardown.
* ``group_into_bursts`` — small helper for batching-style assertions.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional


# ---------------------------------------------------------------------------
# HERMES_HOME allocation (session-scoped tmp dir)
# ---------------------------------------------------------------------------

def make_session_hermes_home(tmp_path_factory) -> Path:
    """Allocate a single HERMES_HOME directory for the whole integration run.

    Mirrors the env scrubs the root conftest does per-function, and resets
    the plugin manager singleton so plugins don't leak from ``~/.hermes``.
    Returns the directory; caller is responsible for restoring the prior
    ``HERMES_HOME`` env var on teardown.
    """
    fake = tmp_path_factory.mktemp("hermes_integration_home")
    for sub in ("sessions", "cron", "memories", "skills"):
        (fake / sub).mkdir(exist_ok=True)
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
    return fake


# ---------------------------------------------------------------------------
# build_skip_if_not_configured factory
# ---------------------------------------------------------------------------

def build_skip_if_not_configured(
    *,
    env_vars: tuple,
    install_extra: str,
    dependency_module: str,
    suite_label: str,
    pytest_path_hint: str,
) -> Callable[[], None]:
    """Return a ``_skip()`` callable for use inside platform fixtures.

    Behaviour, in order:

    1. If any required env var is missing, ``pytest.skip()`` with a list
       of which ones.
    2. If ``dependency_module`` (e.g. ``"telethon"``, ``"discord"``)
       cannot be imported, skip with the install hint.
    3. If running under pytest-xdist on any worker other than the first
       (``gw0``), skip with a hint to re-run with ``-n 0`` — only one
       polling consumer per bot token / Discord client is allowed.
    """
    import pytest

    def _skip() -> None:
        missing = [k for k in env_vars if not os.getenv(k)]
        if missing:
            pytest.skip(
                f"{suite_label} integration tests require env vars: "
                + ", ".join(missing)
                + ". See tests/integration/README.md."
            )
        try:
            __import__(dependency_module)
        except ImportError:
            pytest.skip(
                f"{dependency_module} not installed; install with: "
                f"uv pip install -e '.[{install_extra}]'"
            )
        worker_id = os.environ.get("PYTEST_XDIST_WORKER")
        if worker_id is not None and worker_id not in ("master", "gw0"):
            pytest.skip(
                f"{suite_label} integration tests must run on a single worker. "
                f"Re-run with: pytest {pytest_path_hint} "
                f"-v -m {suite_label.lower()}_integration -n 0 -rs"
            )

    return _skip


# ---------------------------------------------------------------------------
# RunnerHarness — boots GatewayRunner on a dedicated thread
# ---------------------------------------------------------------------------

class RunnerHarness:
    """Run a ``GatewayRunner`` on its own background thread + event loop.

    Adapter polling loops (python-telegram-bot, discord.py) take over the
    loop they were started on.  Running on a dedicated thread keeps them
    out of pytest-asyncio's loop, which is needed so the test client
    (Telethon, discord.py driver) can share the main loop with tests.
    Cross-thread communication happens only through the network — never
    through shared Python state.
    """

    def __init__(self) -> None:
        self.runner = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None

    def start(self, platforms: Dict[Any, Any]) -> None:
        """Boot the gateway with the given ``{Platform: PlatformConfig}`` map."""
        def _thread_main() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.loop = loop
                from gateway.config import GatewayConfig
                from gateway.run import GatewayRunner

                config = GatewayConfig(platforms=platforms)
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
        # If the gateway thread's loop has already stopped (e.g. because
        # runner.start() raised mid-connect on a bad token), scheduling
        # coroutines against it via run_coroutine_threadsafe never
        # executes them — that's what produces the "coroutine was never
        # awaited" warnings.  Only schedule shutdown work on a live loop.
        if self.loop.is_running() and not self.loop.is_closed():
            try:
                fut = asyncio.run_coroutine_threadsafe(self.runner.stop(), self.loop)
                fut.result(timeout=30)
            except Exception:
                pass
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
            await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# BotChat — platform-agnostic conversation helper
# ---------------------------------------------------------------------------

# Default text extractor handles both Telethon (``message`` attr) and
# Discord (``content`` attr) message objects.  Tests can override.
def _default_text_extractor(m: Any) -> str:
    text = getattr(m, "message", None)
    if text is None:
        text = getattr(m, "content", "")
    return text or ""


# Transient status messages the gateway emits *before* the real reply.
# BotChat.expect_reply_messages keeps waiting past these until either a
# real reply arrives or the timeout expires.  Without this, a test that
# fires "/help" during an in-flight agent run would see just the
# "Interrupting current task" ack (gateway/run.py:1411-1414) and return
# prematurely, missing the actual /help content that arrives seconds
# later once the interrupt completes.
_TRANSIENT_ACK_SUBSTRINGS = (
    "Interrupting current task",
    "queued for the next turn",
    "is not accepting another turn",
)


def _is_transient_ack(text: str) -> bool:
    return any(s in text for s in _TRANSIENT_ACK_SUBSTRINGS)


class BotChat:
    """Per-test conversation wrapper.

    Inbound messages arrive via an externally-managed ``asyncio.Queue``
    (the platform-specific fixture owns handler registration so it can
    filter to the right peer / channel and tear down cleanly).  Outbound
    messages go via an injected ``send_callback`` coroutine.

    Test API mirrors the original Telegram-only ``BotChat``:

    * ``drain()``                — clear the inbound queue
    * ``send(text)``             — drain, then send
    * ``expect_reply_messages``  — wait for at least one reply, then
                                   coalesce follow-up chunks within a
                                   ``settle`` window. Returns raw
                                   message objects so platform tests can
                                   inspect entities/embeds.
    * ``expect_reply``           — text-only convenience
    * ``send_and_expect``        — send then await
    * ``collect_replies``        — collect everything in a window
                                   (returns ``[]`` if nothing arrives)
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue,
        send_callback: Callable[[str], Awaitable[Any]],
        text_extractor: Optional[Callable[[Any], str]] = None,
    ) -> None:
        self._queue = queue
        self._send = send_callback
        self._text = text_extractor or _default_text_extractor

    async def drain(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def send(self, text: str):
        await self.drain()
        return await self._send(text)

    async def send_raw(self, text: str):
        """Send without draining the inbound queue.

        Used by the rapid-burst batching test, which needs to keep replies
        from earlier sends (and the in-flight ones) in the queue so the
        per-burst coalescing can be observed.
        """
        return await self._send(text)

    async def expect_reply_messages(
        self, timeout: float = 20.0, settle: float = 1.5,
    ) -> list:
        # Skip past gateway transient-ack messages ("Interrupting
        # current task…", "queued for the next turn…") that arrive
        # before the real reply.  We re-arm the full ``timeout`` for
        # each skip because the real reply can take a while to arrive
        # after an interrupt completes.
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    "expect_reply_messages: timeout exhausted waiting "
                    "for a non-transient reply"
                )
            first = await asyncio.wait_for(self._queue.get(), remaining)
            if _is_transient_ack(self._text(first)):
                # Re-arm timeout for the real reply after an interrupt ack.
                deadline = time.monotonic() + timeout
                continue
            break
        messages = [first]
        while True:
            try:
                extra = await asyncio.wait_for(self._queue.get(), settle)
                # Drop any transient acks that arrive *after* the real
                # reply as well — they're only noise at that point.
                if _is_transient_ack(self._text(extra)):
                    continue
                messages.append(extra)
            except asyncio.TimeoutError:
                break
        return messages

    async def expect_reply(self, timeout: float = 20.0, settle: float = 1.5) -> str:
        messages = await self.expect_reply_messages(timeout=timeout, settle=settle)
        return "\n".join(self._text(m) for m in messages)

    async def send_and_expect(self, text: str, timeout: float = 20.0) -> str:
        await self.send(text)
        return await self.expect_reply(timeout=timeout)

    async def collect_replies(self, window: float = 5.0, settle: float = 1.5) -> list:
        deadline = time.monotonic() + window
        messages: list = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                wait = settle if messages else remaining
                msg = await asyncio.wait_for(self._queue.get(), min(wait, remaining))
                # Drop transient acks so the batching test counts real
                # reply bursts, not interrupt noise.
                if _is_transient_ack(self._text(msg)):
                    continue
                messages.append(msg)
            except asyncio.TimeoutError:
                if messages:
                    break
                break
        return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def group_into_bursts(messages: list, gap: float, *, date_attr: str = "date") -> list:
    """Group consecutive messages into bursts whose gaps are < ``gap`` seconds.

    Both Telethon ``Message`` and discord.py ``Message`` expose a ``date``
    / ``created_at`` attribute that's a ``datetime``.  Default is ``date``;
    Discord callers pass ``date_attr="created_at"``.
    """
    if not messages:
        return []
    bursts = [[messages[0]]]
    for prev, curr in zip(messages, messages[1:]):
        prev_t = getattr(prev, date_attr)
        curr_t = getattr(curr, date_attr)
        delta = (curr_t - prev_t).total_seconds()
        if delta <= gap:
            bursts[-1].append(curr)
        else:
            bursts.append([curr])
    return bursts
