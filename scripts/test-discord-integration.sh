#!/usr/bin/env bash
# Run the real-network Discord integration suite.
#
# Boots a real Hermes gateway against a throwaway test bot ("gateway-under-test")
# and drives it from a *second* Discord bot ("driver") posting into a dedicated
# test guild channel over the live Discord network.  See
# tests/integration/README.md for one-time setup (two bots in the Discord
# Developer Portal, a private test guild with both invited, env vars).
#
# `-n 0` is required: discord.py opens one gateway WebSocket per token, and
# tests share session state — multi-worker runs would conflict.
#
# Usage:
#   ./scripts/test-discord-integration.sh                    # full suite
#   ./scripts/test-discord-integration.sh -k test_smoke_help # one test by name
#   ./scripts/test-discord-integration.sh -k yolo            # filter by name
#   ./scripts/test-discord-integration.sh \
#       tests/integration/discord/test_discord_integration.py::test_smoke_help
#
# Any extra args are forwarded to pytest.

set -euo pipefail

cd "$(dirname "$0")/.."

# Required env vars — fail loudly if any are missing.  Without this check
# pytest would "skip cleanly", which is the right default for CI (where
# missing secrets mean the job wasn't configured) but misleading when
# you're running locally and expect the suite to execute.
required_vars=(
    DISCORD_TEST_GATEWAY_BOT_TOKEN
    DISCORD_TEST_DRIVER_BOT_TOKEN
    DISCORD_TEST_GUILD_ID
    DISCORD_TEST_CHANNEL_ID
    DISCORD_TEST_GATEWAY_BOT_USER_ID
)
missing=()
for v in "${required_vars[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        missing+=("$v")
    fi
done
if (( ${#missing[@]} > 0 )); then
    echo "error: missing required env vars for Discord integration tests:" >&2
    for v in "${missing[@]}"; do
        echo "  - $v" >&2
    done
    echo "" >&2
    echo "See tests/integration/README.md § 'Discord integration setup'." >&2
    exit 1
fi

exec uv run pytest \
    tests/integration/discord/ \
    -v -m discord_integration -n 0 -rs \
    "$@"
