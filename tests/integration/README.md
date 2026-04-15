# Integration tests

This directory holds tests that talk to **real external services**. They are excluded from the default `pytest` run via `addopts = "-m 'not integration and not telegram_integration'"` in `pyproject.toml`, so they never execute unless you opt in with a marker.

| File | Marker | Requires |
|------|--------|----------|
| `telegram/test_telegram_integration.py` | `telegram_integration` | A real Telegram bot + a Telethon user session (see below) |
| `discord/test_discord_integration.py` | `discord_integration` | Two real Discord bots + a private test guild (see below) |
| `test_batch_runner.py`, `test_modal_terminal.py`, … | `integration` | Various API keys |

## Telegram integration setup (one-time)

The Telegram integration suite boots a real `GatewayRunner` with a dedicated bot token, then drives it from a Telethon user account over the live Telegram network. The round-trip exercises the full stack: `python-telegram-bot`, polling, MarkdownV2 escaping, batching, and the base-adapter pipeline.

### 1. Create a new Telegram bot

Talk to [@BotFather](https://t.me/BotFather) on Telegram:

1. `/newbot` → name + username (e.g. `@my_hermes_test_bot`)
2. Copy the token it gives you → this is `TELEGRAM_TEST_BOT_TOKEN`
3. `/setprivacy` → **Disable** (so the bot sees all group messages; required for later stages that test group behavior)

Don't reuse bot tokens since the tests will call `/new`, `/yolo`, etc. and can disturb live sessions.

### 2. Obtain Telegram API credentials

Telethon needs user-level MTProto credentials separate from the bot token. Log in at [my.telegram.org](https://my.telegram.org/) → **API development tools** → create an app. You get:

- `API_ID` (integer) → `TELEGRAM_TEST_API_ID`
- `API_HASH` (string) → `TELEGRAM_TEST_API_HASH`

### 3. Generate a Telethon session string

Use a dedicated "test user" Telegram account (a second phone number, ideally). Running the helper will prompt for the API creds and a login code sent to that account:

```bash
uv pip install -e '.[telegram-integration]'
python scripts/gen_telethon_session.py
```

This will prompt you for phone number, password, 2FA, etc. so it's recommended to use a new account.

It prints a `TELEGRAM_TEST_SESSION_STRING=…` line. Copy the value into your env / CI secret. **Treat it like a password** as it grants full access to that account.

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

The `gateway_runner` fixture sets `GATEWAY_ALLOW_ALL_USERS=true` for the duration of the suite, so the test user is recognized without asking for pairing code etc.. This is safe because the test bot is throwaway, don't use this flag on a real bot.

### 7. Make sure no other process is polling the same bot

Telegram allows only one polling consumer per bot token. If your dev gateway is running with the test token, stop it before the suite runs otherwise you'll see `Conflict: terminated by other getUpdates` errors.

### 8. Run the suite

```bash
./scripts/test-telegram-integration.sh
```

That's a thin wrapper around:

```bash
uv run pytest tests/integration/telegram/ -v -m telegram_integration -n 0 -rs
```

Forward extra args to pytest by passing them to the script (e.g. `./scripts/test-telegram-integration.sh -k yolo` or `./scripts/test-telegram-integration.sh -k test_smoke_help`). To select a single test by full nodeid, pass the file path explicitly: `./scripts/test-telegram-integration.sh tests/integration/telegram/test_telegram_integration.py::test_smoke_help`.

**`-n 0` is required.** The default `addopts` in `pyproject.toml` runs pytest under xdist with multiple workers, but Telegram only allows one polling consumer per bot token — running multiple workers would fight over `getUpdates` and crash. `-n 0` disables xdist for this invocation. Tests within the suite are also intentionally sequential (they share session state for `/yolo`, `/new`, etc.).

Without the env vars set, the tests **skip cleanly** — this is the expected local behavior when you haven't set up a test bot yet.

## Running in CI

### One-time CI setup (maintainer)

1. Create a GitHub Environment named **`telegram-integration`** under *Settings → Environments → New environment*.
2. In that environment, add the five secrets from step 4 above (`TELEGRAM_TEST_BOT_TOKEN`, `TELEGRAM_TEST_API_ID`, `TELEGRAM_TEST_API_HASH`, `TELEGRAM_TEST_SESSION_STRING`, `TELEGRAM_TEST_BOT_USERNAME`).
3. *Strongly recommended:* enable **Required reviewers** on the environment and add at least one maintainer other than yourself. Each `workflow_dispatch` run will then pause until a different maintainer clicks *Approve and run* before the secrets unlock.
4. *Strongly recommended:* use a dedicated throwaway Telegram account for the Telethon session, not your personal account. If the session string ever leaks, an attacker gets full access to that account.

### Triggering a run

*Actions tab → Tests workflow → Run workflow → choose branch → Run workflow.* The job will:
- Skip the `test` and `e2e` jobs (those still run automatically on push/PR; `workflow_dispatch` is reserved for `telegram-integration`).
- Pause for environment approval if you configured required reviewers.
- Run `pytest tests/integration/telegram/ -v -m telegram_integration -n 0 -rs` against the chosen ref.
- 
## Discord integration setup (one-time)

The Discord integration suite uses **two real Discord bots** running in the same pytest process:

* The *gateway-under-test* — a normal Hermes Discord adapter, booted by the test harness with one of the throwaway tokens.
* The *driver* — a second discord.py bot that posts test messages into a dedicated channel and captures the gateway bot's replies via an `on_message` listener.

We use two bots because Discord prohibits selfbots (and has actively blocked them since 2021), so the Telegram approach of driving the bot from a user account does not translate.

### 1. Create both bots in the Discord Developer Portal

For each of `gateway-bot` and `driver-bot`:

1. Go to <https://discord.com/developers/applications> → **New Application** → name it (e.g. `hermes-test-gateway`, `hermes-test-driver`).
2. **Bot** tab → **Reset Token** → copy. These become `DISCORD_TEST_GATEWAY_BOT_TOKEN` and `DISCORD_TEST_DRIVER_BOT_TOKEN`.
3. **Bot** tab → **Privileged Gateway Intents** → enable **Message Content Intent** (required for both bots — without it the driver's `on_message` will fire with empty content and every assertion will fail).
4. **OAuth2 → URL Generator** → scopes: `bot`. Permissions: `Send Messages`, `Read Message History`, `View Channels`. Copy the generated URL and visit it to invite the bot to your test guild (see step 2).

Don't reuse production bot tokens — the suite calls `/new`, `/yolo`, etc. and will disturb live sessions.

### 2. Create a dedicated test guild

In the Discord client (your normal user account):

1. Server picker → **Add a Server → Create My Own → For me and my friends**. Name it something like `hermes-integration-tests`.
2. Invite both bots to it via the OAuth2 URLs from step 1.4.
3. Create a text channel (e.g. `#bot-tests`) — both bots will need View + Send permissions in it (the default permissions cover this).

### 3. Grab the IDs

Enable Discord developer mode: **User Settings → Advanced → Developer Mode** (toggle on). Now right-click any guild/channel/user and **Copy ID**.

* Right-click the test guild in the server picker → Copy Server ID → `DISCORD_TEST_GUILD_ID`
* Right-click `#bot-tests` → Copy Channel ID → `DISCORD_TEST_CHANNEL_ID`
* Right-click the gateway-under-test bot in the member list → Copy User ID → `DISCORD_TEST_GATEWAY_BOT_USER_ID` (the driver's `on_message` listener filters on this so it only queues the gateway bot's replies)

### 4. Configure env vars

Locally, add to `~/.hermes/.env` (or your shell profile):

```bash
export DISCORD_TEST_GATEWAY_BOT_TOKEN=MTIzNDU2...
export DISCORD_TEST_DRIVER_BOT_TOKEN=Nzg5MDEy...
export DISCORD_TEST_GUILD_ID=123456789012345678
export DISCORD_TEST_CHANNEL_ID=234567890123456789
export DISCORD_TEST_GATEWAY_BOT_USER_ID=345678901234567890
```

### 5. Authorization & gating

The `gateway_runner` fixture sets these env overrides for the suite:

* `GATEWAY_ALLOW_ALL_USERS=true` — the driver bot bypasses the auth allowlist.
* `DISCORD_ALLOW_BOTS=all` — the gateway accepts messages from another bot (default `none` would silently ignore the driver).
* `DISCORD_IGNORE_NO_MENTION=false` — the gateway processes channel messages without requiring an `@mention` (default `true` would force `@gateway-bot /help` on every send).

Don't set these flags on a production bot — they're only safe in the throwaway test guild.

### 6. Make sure no other process is using the same tokens

discord.py opens one gateway WebSocket per token. If a dev gateway is already connected with the test gateway-bot token, stop it before running the suite.

### 7. Run the suite

```bash
./scripts/test-discord-integration.sh
```

That's a thin wrapper around:

```bash
uv run pytest tests/integration/discord/ -v -m discord_integration -n 0 -rs
```

Forward extra args to pytest by passing them to the script (e.g. `./scripts/test-discord-integration.sh -k yolo` or `./scripts/test-discord-integration.sh -k test_smoke_help`). To select a single test by full nodeid, pass the file path explicitly: `./scripts/test-discord-integration.sh tests/integration/discord/test_discord_integration.py::test_smoke_help`.

`-n 0` is required for the same reasons as the Telegram suite — only one client per token, and tests share session state and run sequentially.

Without the env vars set, the tests **skip cleanly** — this is the expected local behavior when you haven't set up the test bots yet.

### CI setup

Mirror the Telegram setup:

1. Create a GitHub Environment named **`discord-integration`** under *Settings → Environments → New environment*.
2. Add the five secrets from step 4 above.
3. *Strongly recommended:* enable **Required reviewers** on the environment and add at least one maintainer other than yourself.
4. *Strongly recommended:* the gateway-under-test bot's token is **suite-scoped** — keep it out of any production guilds.

Trigger via *Actions tab → Tests workflow → Run workflow → choose branch*. The job will pause for environment approval (if configured) before the secrets unlock.

## Troubleshooting

### Telegram

- **`AuthKeyUnregisteredError` / session not authorized** — regenerate the session with `scripts/gen_telethon_session.py`; sessions expire after long inactivity.
- **`GatewayRunner.start() returned False`** — bot token is invalid or another process is polling it.
- **Test hangs for ~20s, then `TimeoutError` on `expect_reply`** — the bot is running but didn't reply. Check the gateway logs; most commonly a MarkdownV2 parse failure is dropping the reply on the floor.
- **`FloodWaitError`** — you've hit a rate limit, usually from running the suite too often. Wait it out; Telegram returns the remaining seconds in the error.

### Discord

- **Every assertion fails with empty `content`** — Message Content Intent is not enabled on the driver bot in the Developer Portal. Toggle it on (no redeploy needed).
- **`Driver bot did not become ready within 60s`** — `DISCORD_TEST_DRIVER_BOT_TOKEN` is wrong, or the driver bot hasn't been invited to the test guild.
- **`Driver bot cannot see channel`** — check `DISCORD_TEST_CHANNEL_ID` and that the driver bot has View permissions in that channel.
- **`/help` and friends never get a reply** — the gateway is silently dropping the driver's messages. The fixture sets five env overrides that together let a bot-authored message in a guild channel reach the command handler: `GATEWAY_ALLOW_ALL_USERS=true`, `DISCORD_ALLOW_BOTS=all`, `DISCORD_REQUIRE_MENTION=false`, `DISCORD_IGNORE_NO_MENTION=false`, `DISCORD_AUTO_THREAD=false`. If you've copied the conftest pattern elsewhere, make sure all five are in place — missing any of them will cause replies to vanish at a different gate.
- **Test reply is just "⚡ Interrupting current task…"** — this is a gateway busy-session ack (gateway/run.py:1411) that fires when a new message arrives while the session still has a claim from a prior command. `BotChat.expect_reply_messages` already skips past these and waits for the real reply; if you're seeing this leak through a test assertion you're probably accessing the queue directly instead of going through `expect_reply` / `send_and_expect`.
- **`GatewayRunner.start() returned False`** — `DISCORD_TEST_GATEWAY_BOT_TOKEN` is invalid, or another process (a dev gateway) is already connected with the same token.
- **`LoginFailure: Improper token has been passed`** — the token string is malformed. Common causes: copied the Application ID (18-20 digits) or Public Key instead of the Bot Token, included a `Bot ` prefix, or copied with trailing whitespace. Verify shape with: `python -c 'import os; t=os.environ["DISCORD_TEST_GATEWAY_BOT_TOKEN"]; print("len:", len(t), "parts:", len(t.split(".")))'` — expect `len: ~70, parts: 3`. If wrong, Reset Token in the Developer Portal and re-export.
