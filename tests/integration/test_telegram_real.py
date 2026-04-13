"""Real-network Telegram integration tests.

These tests boot a real ``GatewayRunner`` with a real bot token and drive it
from a real Telethon user account over the live Telegram network. They are
gated behind the ``real_telegram`` pytest marker (excluded by default via
``addopts`` in pyproject.toml) and the ``TELEGRAM_TEST_*`` env vars.

Run locally with all env vars set:

    uv pip install -e '.[all,dev,real-tests]'
    pytest tests/integration/test_telegram_real.py -v -m real_telegram

See ``tests/integration/README.md`` for one-time setup.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.real_telegram]


@pytest.mark.asyncio(loop_scope="session")
async def test_smoke_help(bot_chat):
    """Stage-0 smoke: /help round-trips through real Telegram."""
    reply = await bot_chat.send_and_expect("/help", timeout=30.0)
    assert "Hermes Commands" in reply, (
        f"/help reply did not contain the expected header. Got: {reply!r}"
    )
