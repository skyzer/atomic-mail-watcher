# Atomic Mail Watcher

Docker-ready Atomic Mail inbox watcher for AI agents.

It uses Atomic Mail's JMAP API to detect new inbox mail, deduplicates messages locally, and can notify Telegram, a generic webhook, or stdout. It is designed for agent inboxes where you want proactive "mail arrived" alerts without giving the watcher permission to auto-reply.

## What this is

- Near-real-time watcher using JMAP `eventSourceUrl` when available.
- Periodic reconciliation loop so dropped SSE connections do not lose alerts.
- One-shot `check` mode for cron/Kubernetes jobs.
- Secret-safe by default: credentials stay in mounted files or env vars; state is local.
- Pure Python stdlib; no runtime package dependencies.
- Docker and Docker Compose included.

## What this is not

- It does not register an Atomic Mail account yet. Create the inbox/API key first, then pass credentials to this watcher.
- It does not send email replies. It only notifies that mail arrived.
- It is not Hermes-specific. Hermes can run it, but any Docker host can run it.

## Quick start with Docker Compose

```bash
cp .env.example .env
mkdir -p data
chmod 700 data
```

Create `data/credentials.json`:

```json
{
  "inboxId": "your-agent@atomicmail.ai",
  "apiKey": "am_...",
  "authUrl": "https://auth.atomicmail.ai",
  "apiUrl": "https://api.atomicmail.ai"
}
```

Edit `.env` with notification settings, for example Telegram:

```bash
TELEGRAM_BOT_TOKEN=123456:abc
TELEGRAM_CHAT_ID=123456789
```

Build and run:

```bash
docker compose up -d --build
```

Follow logs:

```bash
docker compose logs -f atomicmail-watcher
```

The first run initializes `data/state.json` with currently visible inbox messages and does **not** alert old mail. New messages after that trigger notifications.

## One-shot check mode

Useful for cron, Kubernetes CronJob, systemd timers, or any platform that expects stdout only when something happened:

```bash
docker compose run --rm atomicmail-watcher --mode check --emit-stdout
```

Initialize state without notifying:

```bash
docker compose run --rm atomicmail-watcher --mode check --initialize-only --verbose
```

## Test notification path

This only tests your notifier credentials; it does not require Atomic Mail credentials:

```bash
docker compose run --rm atomicmail-watcher --mode test-notifier --send-telegram
```

## Configuration

All CLI flags have environment-variable equivalents where it matters.

| Setting | CLI | Environment | Default |
|---|---|---|---|
| Data dir | `--data-dir` | `DATA_DIR` | `/data` |
| Credentials file | `--credentials-file` | `ATOMICMAIL_CREDENTIALS_FILE` | `/data/credentials.json` |
| State file | `--state-file` | `STATE_FILE` | `/data/state.json` |
| Log file | `--log-file` | `LOG_FILE` | `/data/watcher.log` |
| Atomic API key | — | `ATOMICMAIL_API_KEY` | from credentials file |
| Atomic inbox ID/email | — | `ATOMICMAIL_INBOX_ID` | from credentials file |
| Atomic auth URL | — | `ATOMICMAIL_AUTH_URL` | `https://auth.atomicmail.ai` |
| Atomic API URL | — | `ATOMICMAIL_API_URL` | `https://api.atomicmail.ai` |
| Telegram bot token | — | `TELEGRAM_BOT_TOKEN` | unset |
| Telegram chat ID | — | `TELEGRAM_CHAT_ID` or `TELEGRAM_HOME_CHANNEL` | unset |
| Webhook URL | — | `WEBHOOK_URL` | unset |
| Webhook bearer | — | `WEBHOOK_BEARER_TOKEN` | unset |

Credential values in environment variables override values from `credentials.json`.

## Notification modes

Telegram:

```bash
python -m atomicmail_watcher --mode watch --send-telegram
```

Generic webhook:

```bash
python -m atomicmail_watcher --mode watch --send-webhook
```

Webhook payload shape:

```json
{
  "inbox": "your-agent@atomicmail.ai",
  "count": 1,
  "text": "markdown summary",
  "messages": [
    {
      "id": "...",
      "subject": "...",
      "from": [{"name": "...", "email": "..."}],
      "receivedAt": "...",
      "preview": "..."
    }
  ]
}
```

Stdout:

```bash
python -m atomicmail_watcher --mode check --emit-stdout
```

## Docker image

```bash
docker build -t atomicmail-watcher:local .
docker run --rm --env-file .env -v "$PWD/data:/data" atomicmail-watcher:local --mode check --verbose
```

## Security notes

- Do not commit `.env`, `data/`, `*.jwt`, or credential JSON files.
- The container runs as a non-root user.
- Session/capability JWTs are stored under the data directory with restrictive file modes where the host allows it.
- Error messages intentionally avoid printing authorization headers.
- The watcher never sends mail.

## Open-source status

This package is suitable to publish as a small MIT-licensed utility. Before publishing, do a final check that your working tree does not contain private `.env`, `data/`, logs, tokens, or user-specific paths.
