#!/usr/bin/env python3
"""Generate a Telethon StringSession for the Telegram integration tests.

Usage:
    uv pip install -e '.[telegram-integration]'
    python scripts/gen_telethon_session.py

You'll be prompted for:
    - API_ID / API_HASH (from https://my.telegram.org → API development tools)
    - phone number
    - the login code Telegram sends to that phone
    - your 2FA password, if enabled

The resulting StringSession is printed to stdout. Copy it into the
TELEGRAM_TEST_SESSION_STRING env var (locally) or GitHub Actions secret (for CI).

Never commit this string — it grants full access to the account.
"""
from __future__ import annotations

import getpass
import os
import sys


def main() -> int:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print(
            "telethon is not installed. Install with:\n"
            "    uv pip install -e '.[telegram-integration]'",
            file=sys.stderr,
        )
        return 1

    api_id = os.getenv("TELEGRAM_TEST_API_ID") or input("API_ID: ").strip()
    api_hash = os.getenv("TELEGRAM_TEST_API_HASH") or getpass.getpass("API_HASH: ").strip()

    try:
        api_id_int = int(api_id)
    except ValueError:
        print(f"API_ID must be an integer, got: {api_id!r}", file=sys.stderr)
        return 1

    # Will ask interactively for phone number, 2fa, etc.
    # https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.auth.AuthMethods.start
    with TelegramClient(StringSession(), api_id_int, api_hash) as client:
        session_str = client.session.save()
        print()
        print("=" * 72)
        print("TELEGRAM_TEST_SESSION_STRING=" + session_str)
        print("=" * 72)
        print("\nPaste the value above into your env / GitHub secret.")
        print("Keep it private — it grants full access to the account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
