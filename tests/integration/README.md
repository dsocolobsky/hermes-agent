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
./scripts/test-telegram-real.sh
```

That's a thin wrapper around:

```bash
uv run pytest tests/integration/test_telegram_real.py -v -m real_telegram -n 0 -rs
```

Forward extra args to pytest by passing them to the script (e.g. `./scripts/test-telegram-real.sh -k yolo` or `./scripts/test-telegram-real.sh ::test_smoke_help`).

**`-n 0` is required.** The default `addopts` in `pyproject.toml` runs pytest under xdist with multiple workers, but Telegram only allows one polling consumer per bot token — running multiple workers would fight over `getUpdates` and crash. `-n 0` disables xdist for this invocation. Tests within the suite are also intentionally sequential (they share session state for `/yolo`, `/new`, etc.).

Without the env vars set, the tests **skip cleanly** — this is the expected local behavior when you haven't set up a test bot yet.

## Running in CI

Triggered manually only — never on push or PR. The `real-telegram` job in `.github/workflows/tests.yml` is gated `if: github.event_name == 'workflow_dispatch'` because the secrets it uses include a Telethon user-account session string with full access to that account; running it on every PR would let any contributor (or a compromised maintainer) exfiltrate the secret with a one-line code change.

### One-time CI setup (maintainer)

1. Create a GitHub Environment named **`real-telegram`** under *Settings → Environments → New environment*.
2. In that environment, add the five secrets from step 4 above (`TELEGRAM_TEST_BOT_TOKEN`, `TELEGRAM_TEST_API_ID`, `TELEGRAM_TEST_API_HASH`, `TELEGRAM_TEST_SESSION_STRING`, `TELEGRAM_TEST_BOT_USERNAME`).
3. *Strongly recommended:* enable **Required reviewers** on the environment and add at least one maintainer other than yourself. Each `workflow_dispatch` run will then pause until a different maintainer clicks *Approve and run* before the secrets unlock — two-eyes principle, defense against a single compromised account.
4. *Strongly recommended:* use a dedicated throwaway Telegram account for the Telethon session, not your personal account. If the session string ever leaks, an attacker gets full access to that account.

### Triggering a run

*Actions tab → Tests workflow → Run workflow → choose branch → Run workflow.* The job will:
- Skip the `test` and `e2e` jobs (those still run automatically on push/PR; `workflow_dispatch` is reserved for `real-telegram`).
- Pause for environment approval if you configured required reviewers.
- Run `pytest tests/integration/test_telegram_real.py -v -m real_telegram -n 0 -rs` against the chosen ref.

### Why fork PRs can't trigger this

GitHub deliberately blocks fork PRs from accessing secrets — there's no way around it, and that's the desired behavior. Maintainers reviewing a fork PR can manually trigger `workflow_dispatch` against the PR's ref after reviewing the diff (Actions tab → Tests → Run workflow → branch dropdown shows PR refs as `refs/pull/N/merge` if you have the right permissions, or push the PR branch into the upstream repo first).

## Troubleshooting

- **`AuthKeyUnregisteredError` / session not authorized** — regenerate the session with `scripts/gen_telethon_session.py`; sessions expire after long inactivity.
- **`GatewayRunner.start() returned False`** — bot token is invalid or another process is polling it.
- **Test hangs for ~20s, then `TimeoutError` on `expect_reply`** — the bot is running but didn't reply. Check the gateway logs; most commonly a MarkdownV2 parse failure is dropping the reply on the floor.
- **`FloodWaitError`** — you've hit a rate limit, usually from running the suite too often. Wait it out; Telegram returns the remaining seconds in the error.
