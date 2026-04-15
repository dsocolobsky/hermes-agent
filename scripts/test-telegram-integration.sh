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
#   ./scripts/test-telegram-integration.sh                    # full suite
#   ./scripts/test-telegram-integration.sh -k test_smoke_help # one test by name
#   ./scripts/test-telegram-integration.sh -k yolo            # filter by name
#   ./scripts/test-telegram-integration.sh \
#       tests/integration/telegram/test_telegram_integration.py::test_smoke_help
#
# Any extra args are forwarded to pytest.

set -euo pipefail

cd "$(dirname "$0")/.."

# Required env vars — fail loudly if any are missing.  Without this check
# pytest would "skip cleanly", which is the right default for CI (where
# missing secrets mean the job wasn't configured) but misleading when
# you're running locally and expect the suite to execute.
required_vars=(
    TELEGRAM_TEST_BOT_TOKEN
    TELEGRAM_TEST_API_ID
    TELEGRAM_TEST_API_HASH
    TELEGRAM_TEST_SESSION_STRING
    TELEGRAM_TEST_BOT_USERNAME
)
missing=()
for v in "${required_vars[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        missing+=("$v")
    fi
done
if (( ${#missing[@]} > 0 )); then
    echo "error: missing required env vars for Telegram integration tests:" >&2
    for v in "${missing[@]}"; do
        echo "  - $v" >&2
    done
    echo "" >&2
    echo "See tests/integration/README.md § 'Telegram integration setup'." >&2
    exit 1
fi

exec uv run pytest \
    tests/integration/telegram/ \
    -v -m telegram_integration -n 0 -rs \
    "$@"
