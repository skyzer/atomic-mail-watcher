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

## Agent compatibility

This project is agent-agnostic. It is not a native plugin for every agent listed below; it is a small notification bridge that any agent/runtime can use if it can run Docker, read stdout, receive a webhook, or watch a chat notification channel.

Representative pairings:

| Agent / runtime | How it fits |
|---|---|
| **Hermes Agent** | Run as a local daemon, cron fallback, or webhook notifier for a Hermes-owned inbox. |
| **OpenClaw** | Run the Docker Compose service next to OpenClaw and route new-mail alerts through stdout, Telegram, or a webhook. |
| **Claude Code** | Use one-shot `check --emit-stdout` in shell workflows, or consume webhook notifications. |
| **OpenAI Codex CLI** | Use the Docker image as a sidecar inbox monitor for coding-agent workflows. |
| **OpenCode** | Run the watcher as a companion service and let OpenCode react to webhook/stdout alerts. |
| **Cursor / Windsurf / VS Code agents** | Use Docker Compose locally and inspect stdout/webhook notifications from the agent workflow. |
| **LangChain / LangGraph agents** | Consume the generic webhook payload or invoke one-shot check mode from a tool. |
| **CrewAI / AutoGen-style agents** | Treat the watcher as an external mailbox event source. |
| **Dify / n8n / Pipedream / Zapier / Make** | Use webhook delivery to trigger workflows when new mail arrives. |
| **Kubernetes / systemd / Nomad / any Docker host** | Run as a long-lived service or periodic one-shot job. |

In short: if an agent can call a command, run a container, or receive an HTTP webhook, it can use this watcher.

## Relationship to Atomic Mail Agentic

This project complements the official [Atomic Mail Agentic](https://github.com/Atomic-Mail/atomic-mail-agentic) repository. It is not a replacement for the official integration stack.

Use **Atomic Mail Agentic** for registration, MCP, AgentSkill, sending/replying, attachments, JMAP presets, LangChain, Dify, n8n, and other first-party integrations. Use **Atomic Mail Watcher** when you want a small Dockerized sidecar that watches an existing inbox and sends proactive notifications.

| Question | Atomic Mail Agentic | Atomic Mail Watcher |
|---|---|---|
| Is it official Atomic Mail code? | Yes — first-party Atomic Mail integration monorepo | No — small community sidecar utility |
| Primary purpose | Give agents a full programmable mailbox capability | Notify when new mail arrives in an existing inbox |
| Creates/registers inboxes? | Yes — PoW signup and credential persistence | Not yet — bring an existing inbox/API key |
| Reads inbox? | Yes — via `jmap_request`, presets, MCP/tools | Yes — focused on new-message detection and dedupe |
| Sends/replies to email? | Yes — send, reply, attachments, raw JMAP | No — deliberately notification-only |
| MCP support | Yes | Not directly |
| AgentSkill / OpenClaw / Hermes skill support | Yes | Not as a native skill; usable beside those agents |
| LangChain / Dify / n8n integrations | Yes | Generic webhook/stdout only |
| Proactive notification daemon | Partial — n8n polling trigger and scheduled-agent guidance | Yes — Docker daemon with JMAP EventSource/SSE plus reconcile loop |
| Telegram notifications | Not a main feature | Yes |
| Docker-first deployment | Not the main shape | Yes |
| Best use | Agent owns and operates an inbox end-to-end | Host/operator gets proactive “new mail arrived” alerts |

Recommended architecture:

```text
Atomic-Mail/atomic-mail-agentic
  = official tools agents use to register, read, send, reply, and manage mail

skyzer/atomic-mail-watcher
  = Docker notification sidecar that wakes humans or agents when new mail appears
```

A practical workflow is: register and operate the inbox with the official Atomic Mail tools, run this watcher next to your agent runtime, then let the user or agent decide whether to inspect, reply, forward, or ignore each notification.

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
docker compose logs -f atomic-mail-watcher
```

The first run initializes `data/state.json` with currently visible inbox messages and does **not** alert old mail. New messages after that trigger notifications.

## One-shot check mode

Useful for cron, Kubernetes CronJob, systemd timers, or any platform that expects stdout only when something happened:

```bash
docker compose run --rm atomic-mail-watcher --mode check --emit-stdout
```

Initialize state without notifying:

```bash
docker compose run --rm atomic-mail-watcher --mode check --initialize-only --verbose
```

## Test notification path

This only tests your notifier credentials; it does not require Atomic Mail credentials:

```bash
docker compose run --rm atomic-mail-watcher --mode test-notifier --send-telegram
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
docker build -t atomic-mail-watcher:local .
docker run --rm --env-file .env -v "$PWD/data:/data" atomic-mail-watcher:local --mode check --verbose
```

## Atomic Mail roadmap / future implementation ideas

This watcher is intentionally small: it only alerts when mail arrives. Atomic Mail's public agent docs describe a broader agent-email stack, and the following pieces are good candidates for later implementation here:

- **Built-in account registration**: wrap Atomic Mail's PoW signup flow so a container can create/recover an `@atomicmail.ai` inbox from a username instead of requiring a pre-created API key.
- **Human-approved send/reply mode**: add optional JMAP `Email/set` + `EmailSubmission/set` helpers for drafting or sending replies, but keep this disabled by default and gated by explicit human approval.
- **Official integration adapters**: expose the watcher as a tool around Atomic Mail's MCP, AgentSkill, LangChain, Dify, and n8n integration patterns instead of only running as a standalone daemon.
- **Custom domains**: Atomic Mail currently advertises custom domains as coming soon. When available, support domain-specific inbox config and document DNS/setup requirements.
- **Stable-release hardening**: Atomic Mail Agentic is currently described as open alpha/free with quotas and rate limits. When the public stable release lands, revisit defaults for backoff, quotas, errors, and auth/token lifetimes.
- **Richer triage**: optionally classify new messages, thread related updates, and send summaries to webhooks without exposing full message bodies unless configured.

References:

- Atomic Mail Agents: <https://atomicmail.io/agents>
- Atomic Mail Agentic docs: <https://atomic-mail.github.io/atomic-mail-agentic/>

## Security notes

- Do not commit `.env`, `data/`, `*.jwt`, or credential JSON files.
- The container runs as a non-root user.
- Session/capability JWTs are stored under the data directory with restrictive file modes where the host allows it.
- Error messages intentionally avoid printing authorization headers.
- The watcher never sends mail.

## Open-source status

MIT-licensed and intended for public use. Before publishing or cutting releases, verify that the working tree does not contain private `.env`, `data/`, logs, tokens, or user-specific paths.
