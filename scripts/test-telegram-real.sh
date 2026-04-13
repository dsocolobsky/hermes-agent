#!/usr/bin/env bash
# Run the real-network Telegram integration suite.
#
# Boots a real Hermes gateway against a throwaway test bot and drives it from
# a Telethon user account over the live Telegram network.  See
# tests/integration/README.md for one-time setup (bot token, my.telegram.org
# API creds, Telethon session string, env vars).
#
# `-n 0` is required: only one process can poll a Telegram bot token at a time,
# so the suite must run on a single worker (overrides pyproject's `-n auto`).
#
# Usage:
#   ./scripts/test-telegram-real.sh                    # all real_telegram tests
#   ./scripts/test-telegram-real.sh ::test_smoke_help  # one test
#   ./scripts/test-telegram-real.sh -k yolo            # filter by name
#
# Any extra args are forwarded to pytest.

set -euo pipefail

cd "$(dirname "$0")/.."

exec uv run pytest \
    tests/integration/test_telegram_real.py \
    -v -m real_telegram -n 0 -rs \
    "$@"
