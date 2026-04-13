# Integration tests

This directory holds tests that talk to **real external services**. They are excluded from the default `pytest` run via `addopts = "-m 'not integration and not real_telegram'"` in `pyproject.toml`, so they never execute unless you opt in with a marker.

| File | Marker | Requires |
|------|--------|----------|
| `test_telegram_real.py` | `real_telegram` | A real Telegram bot + a Telethon user session (see below) |
| `test_batch_runner.py`, `test_modal_terminal.py`, … | `integration` | Various API keys |

## Real-Telegram setup (one-time)

The real-Telegram suite boots a real `GatewayRunner` with a dedicated bot token, then drives it from a Telethon user account over the live Telegram network. The round-trip exercises the full stack: `python-telegram-bot`, polling, MarkdownV2 escaping, batching, and the base-adapter pipeline.

### 1. Create a throwaway bot

Talk to [@BotFather](https://t.me/BotFather) on Telegram:

1. `/newbot` → name + username (e.g. `@my_hermes_test_bot`)
2. Copy the token it gives you → this is `TELEGRAM_TEST_BOT_TOKEN`
3. `/setprivacy` → **Disable** (so the bot sees all group messages; required for later stages that test group behavior)

**Never reuse a production bot token** — the tests will call `/new`, `/yolo`, etc. and can disturb live sessions.

### 2. Obtain Telegram API credentials

Telethon needs user-level MTProto credentials separate from the bot token. Log in at [my.telegram.org](https://my.telegram.org/) → **API development tools** → create an app. You get:

- `API_ID` (integer) → `TELEGRAM_TEST_API_ID`
- `API_HASH` (string) → `TELEGRAM_TEST_API_HASH`

### 3. Generate a Telethon session string

Use a dedicated "test user" Telegram account (a second phone number, ideally). Running the helper will prompt for the API creds and a login code sent to that account:

```bash
uv pip install -e '.[real-tests]'
python scripts/gen_telethon_session.py
```

It prints a `TELEGRAM_TEST_SESSION_STRING=…` line. Copy the value into your env / CI secret. **Treat it like a password** — it grants full access to that account.

### 4. Configure env vars

Locally, add to `~/.hermes/.env` (or your shell profile):

```bash
export TELEGRAM_TEST_BOT_TOKEN=123456:ABCdef...
export TELEGRAM_TEST_API_ID=1234567
export TELEGRAM_TEST_API_HASH=abcdef0123456789...
export TELEGRAM_TEST_SESSION_STRING=1BVtsOK...
export TELEGRAM_TEST_BOT_USERNAME=my_hermes_test_bot   # without the leading @
```

### 5. Start the test user's conversation with the bot

Open Telegram on the test-user account and send any message to `@my_hermes_test_bot` (e.g. "hi"). This creates the chat entity so Telethon can resolve it.

### 6. Authorization

The `gateway_runner` fixture sets `GATEWAY_ALLOW_ALL_USERS=true` for the duration of the suite, so the test user is recognized without a pairing dance. This is safe because the test bot is throwaway — don't use this flag on a real bot.

If you see a `Hi~ I don't recognize you yet! Here's your pairing code: …` reply, it means the runner was started without that env override (e.g. you spun up `hermes gateway run` manually against the test token instead of using the fixture). Kill that and re-run the suite.

### 7. Make sure no other process is polling the same bot

Telegram allows only one polling consumer per bot token. If your dev gateway is running with the test token, stop it before the suite runs — otherwise you'll see `Conflict: terminated by other getUpdates` errors.

### 8. Run the suite

```bash
pytest tests/integration/test_telegram_real.py -v -m real_telegram -n 0
```

**`-n 0` is required.** The default `addopts` in `pyproject.toml` runs pytest under xdist with multiple workers, but Telegram only allows one polling consumer per bot token — running multiple workers would fight over `getUpdates` and crash. `-n 0` disables xdist for this invocation. Tests within the suite are also intentionally sequential (they share session state for `/yolo`, `/new`, etc.).

Without the env vars set, the tests **skip cleanly** — this is expected and is how CI runs for fork PRs without access to secrets.

## Troubleshooting

- **`AuthKeyUnregisteredError` / session not authorized** — regenerate the session with `scripts/gen_telethon_session.py`; sessions expire after long inactivity.
- **`GatewayRunner.start() returned False`** — bot token is invalid or another process is polling it.
- **Test hangs for ~20s, then `TimeoutError` on `expect_reply`** — the bot is running but didn't reply. Check the gateway logs; most commonly a MarkdownV2 parse failure is dropping the reply on the floor.
- **`FloodWaitError`** — you've hit a rate limit, usually from running the suite too often. Wait it out; Telegram returns the remaining seconds in the error.
