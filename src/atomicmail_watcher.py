#!/usr/bin/env python3
"""Docker-friendly Atomic Mail JMAP inbox watcher.

The watcher can run once (cron style) or stay connected to JMAP EventSource/SSE.
It never sends email; it only notifies when new inbox messages appear.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hashlib
import html
import json
import os
import random
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
DEFAULT_AUTH_URL = "https://auth.atomicmail.ai"
DEFAULT_API_URL = "https://api.atomicmail.ai"
DEFAULT_POW_SCRYPT_SALT_HEX = "0b980734412c292d6549110276b604ab1dea4883bd460d77d1b984adf8bca083"
USER_AGENT = "atomic-mail-watcher/0.1"


class AtomicMailError(RuntimeError):
    """Operational error safe to show without secrets."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def env_get(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AtomicMailError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AtomicMailError(f"expected JSON object in {path}")
    return data


def read_text_if_exists(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def write_secret_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    chmod_best_effort(tmp, 0o600)
    tmp.replace(path)
    chmod_best_effort(path, 0o600)


@dataclasses.dataclass
class Config:
    data_dir: Path
    credentials_file: Path
    state_file: Path
    log_file: Path
    token_dir: Path
    auth_url: str
    api_url: str
    api_key: str
    inbox_id: str
    scrypt_salt: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        data_dir = Path(args.data_dir or env_get("DATA_DIR") or "/data")
        credentials_file = Path(
            args.credentials_file or env_get("ATOMICMAIL_CREDENTIALS_FILE") or data_dir / "credentials.json"
        )
        raw = read_json_file(credentials_file)

        api_key = env_get("ATOMICMAIL_API_KEY") or raw.get("apiKey")
        inbox_id = env_get("ATOMICMAIL_INBOX_ID") or raw.get("inboxId") or raw.get("email")
        if not api_key:
            raise AtomicMailError("missing Atomic Mail API key; set ATOMICMAIL_API_KEY or credentials.json apiKey")
        if not inbox_id:
            raise AtomicMailError("missing Atomic Mail inbox; set ATOMICMAIL_INBOX_ID or credentials.json inboxId")

        state_file = Path(args.state_file or env_get("STATE_FILE") or data_dir / "state.json")
        log_file = Path(args.log_file or env_get("LOG_FILE") or data_dir / "watcher.log")
        token_dir = Path(args.token_dir or env_get("TOKEN_DIR") or data_dir)

        return cls(
            data_dir=data_dir,
            credentials_file=credentials_file,
            state_file=state_file,
            log_file=log_file,
            token_dir=token_dir,
            auth_url=str(env_get("ATOMICMAIL_AUTH_URL") or raw.get("authUrl") or DEFAULT_AUTH_URL).rstrip("/"),
            api_url=str(env_get("ATOMICMAIL_API_URL") or raw.get("apiUrl") or DEFAULT_API_URL).rstrip("/"),
            api_key=str(api_key),
            inbox_id=str(inbox_id),
            scrypt_salt=str(env_get("ATOMICMAIL_SCRYPT_SALT") or raw.get("scryptSalt") or DEFAULT_POW_SCRYPT_SALT_HEX),
        )

    def public_summary(self) -> dict[str, str]:
        return {
            "inbox": inbox_email(self.inbox_id),
            "auth_url": self.auth_url,
            "api_url": self.api_url,
            "credentials_file": str(self.credentials_file),
            "state_file": str(self.state_file),
            "log_file": str(self.log_file),
            "token_dir": str(self.token_dir),
        }


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], Any, str]:
    data: bytes | None = None
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
            parsed: Any = None
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
            return res.status, dict(res.headers.items()), parsed, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = None
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        return exc.code, dict(exc.headers.items()), parsed, raw


def bearer(headers: dict[str, str], label: str) -> str:
    header = headers.get("Authorization") or headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        raise AtomicMailError(f"missing {label} bearer token")
    return header.split(" ", 1)[1].strip()


def decode_jwt_payload(jwt: str) -> dict[str, Any]:
    try:
        payload = jwt.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception as exc:  # noqa: BLE001
        raise AtomicMailError(f"could not decode JWT payload: {exc}") from exc
    if not isinstance(decoded, dict):
        raise AtomicMailError("JWT payload is not an object")
    return decoded


def jwt_expired(jwt: str | None, margin_seconds: int) -> bool:
    if not jwt:
        return True
    payload = decode_jwt_payload(jwt)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return True
    return time.time() + margin_seconds >= float(exp)


def leading_zero_bits(digest: bytes, bits: int) -> bool:
    full_bytes = bits // 8
    remaining = bits % 8
    if any(byte != 0 for byte in digest[:full_bytes]):
        return False
    if remaining:
        mask = (0xFF << (8 - remaining)) & 0xFF
        return (digest[full_bytes] & mask) == 0
    return True


def solve_pow(challenge: str, difficulty: int, salt_text: str) -> tuple[str, str, float]:
    salt = salt_text.encode("utf-8")
    nonce = 0
    started = time.time()
    while True:
        digest = hashlib.scrypt(
            f"{challenge}:{nonce}".encode("utf-8"),
            salt=salt,
            n=16384,
            r=8,
            p=1,
            dklen=64,
            maxmem=128 * 1024 * 1024,
        )
        if leading_zero_bits(digest, difficulty):
            return digest.hex(), str(nonce), time.time() - started
        nonce += 1


class AtomicMailClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session_file = config.token_dir / "session.jwt"
        self.capability_file = config.token_dir / "capability.jwt"
        self._session: dict[str, Any] | None = None
        self._account_id: str | None = None
        self._inbox_mailbox_id: str | None = None
        self._jmap_api_url: str | None = None
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.token_dir.mkdir(parents=True, exist_ok=True)
        chmod_best_effort(config.data_dir, 0o700)
        chmod_best_effort(config.token_dir, 0o700)

    def ensure_session_jwt(self) -> str:
        existing = read_text_if_exists(self.session_file)
        if existing and not jwt_expired(existing, margin_seconds=300):
            return existing
        status, headers, _parsed, raw = http_json("POST", f"{self.config.auth_url}/api/v1/challenge")
        if status >= 400:
            raise AtomicMailError(f"challenge failed {status}: {raw[:300]}")
        challenge_jwt = bearer(headers, "challenge")
        payload = decode_jwt_payload(challenge_jwt)
        challenge = payload.get("jti")
        difficulty = payload.get("difficulty")
        if not isinstance(challenge, str) or not isinstance(difficulty, int):
            raise AtomicMailError("challenge JWT missing jti/difficulty")
        pow_hex, nonce, _seconds = solve_pow(challenge, difficulty, self.config.scrypt_salt)
        body = {"powHex": pow_hex, "nonce": nonce, "apiKey": self.config.api_key}
        status, headers, _parsed, raw = http_json(
            "POST",
            f"{self.config.auth_url}/api/v1/session",
            headers={"Authorization": f"Bearer {challenge_jwt}"},
            body=body,
        )
        if status >= 400:
            raise AtomicMailError(f"session failed {status}: {raw[:300]}")
        session_jwt = bearer(headers, "session")
        write_secret_file(self.session_file, session_jwt)
        return session_jwt

    def ensure_capability_jwt(self) -> str:
        existing = read_text_if_exists(self.capability_file)
        if existing and not jwt_expired(existing, margin_seconds=30):
            return existing
        session_jwt = self.ensure_session_jwt()
        status, headers, _parsed, raw = http_json(
            "POST",
            f"{self.config.auth_url}/api/v1/capability",
            headers={"Authorization": f"Bearer {session_jwt}"},
        )
        if status >= 400:
            try:
                self.session_file.unlink()
            except FileNotFoundError:
                pass
            session_jwt = self.ensure_session_jwt()
            status, headers, _parsed, raw = http_json(
                "POST",
                f"{self.config.auth_url}/api/v1/capability",
                headers={"Authorization": f"Bearer {session_jwt}"},
            )
        if status >= 400:
            raise AtomicMailError(f"capability failed {status}: {raw[:300]}")
        cap = bearer(headers, "capability")
        write_secret_file(self.capability_file, cap)
        return cap

    def jmap_session(self, *, force: bool = False) -> dict[str, Any]:
        if self._session is not None and not force:
            return self._session
        cap = self.ensure_capability_jwt()
        status, _headers, parsed, raw = http_json(
            "GET",
            f"{self.config.api_url}/.well-known/jmap",
            headers={"Authorization": f"Bearer {cap}"},
        )
        if status == 401:
            try:
                self.capability_file.unlink()
            except FileNotFoundError:
                pass
            cap = self.ensure_capability_jwt()
            status, _headers, parsed, raw = http_json(
                "GET",
                f"{self.config.api_url}/.well-known/jmap",
                headers={"Authorization": f"Bearer {cap}"},
            )
        if status >= 400 or not isinstance(parsed, dict):
            raise AtomicMailError(f"JMAP session failed {status}: {raw[:300]}")
        self._session = parsed
        self._jmap_api_url = str(parsed.get("apiUrl") or f"{self.config.api_url}/jmap/")
        self._account_id = self.extract_account_id(parsed)
        return parsed

    def extract_account_id(self, session: dict[str, Any]) -> str:
        primary = session.get("primaryAccounts") or {}
        if isinstance(primary, dict) and primary.get(MAIL):
            return str(primary[MAIL])
        accounts = session.get("accounts") or {}
        if isinstance(accounts, dict) and len(accounts) == 1:
            return next(iter(accounts))
        if isinstance(accounts, dict):
            for account_id, account in accounts.items():
                caps = (account or {}).get("accountCapabilities") or {}
                if MAIL in caps:
                    return str(account_id)
        return self.config.inbox_id.split("@", 1)[0]

    @property
    def account_id(self) -> str:
        if not self._account_id:
            self.jmap_session()
        assert self._account_id is not None
        return self._account_id

    @property
    def jmap_api_url(self) -> str:
        if not self._jmap_api_url:
            self.jmap_session()
        assert self._jmap_api_url is not None
        return self._jmap_api_url

    def jmap(self, method_calls: list[list[Any]]) -> list[list[Any]]:
        cap = self.ensure_capability_jwt()
        body = {"using": [CORE, MAIL], "methodCalls": method_calls}
        status, _headers, parsed, raw = http_json(
            "POST",
            self.jmap_api_url,
            headers={"Authorization": f"Bearer {cap}"},
            body=body,
        )
        if status == 401:
            try:
                self.capability_file.unlink()
            except FileNotFoundError:
                pass
            cap = self.ensure_capability_jwt()
            status, _headers, parsed, raw = http_json(
                "POST",
                self.jmap_api_url,
                headers={"Authorization": f"Bearer {cap}"},
                body=body,
            )
        if status >= 400 or not isinstance(parsed, dict):
            raise AtomicMailError(f"JMAP call failed {status}: {raw[:500]}")
        responses = parsed.get("methodResponses")
        if not isinstance(responses, list):
            raise AtomicMailError(f"JMAP response missing methodResponses: {raw[:500]}")
        return responses

    def inbox_mailbox_id(self) -> str:
        if self._inbox_mailbox_id:
            return self._inbox_mailbox_id
        responses = self.jmap([["Mailbox/get", {"accountId": self.account_id}, "m0"]])
        for name, payload, _tag in responses:
            if name != "Mailbox/get":
                continue
            for mailbox in payload.get("list", []) or []:
                role = mailbox.get("role")
                name_value = str(mailbox.get("name", "")).lower()
                if role == "inbox" or name_value == "inbox":
                    self._inbox_mailbox_id = str(mailbox["id"])
                    return self._inbox_mailbox_id
        raise AtomicMailError("could not find Inbox mailbox")

    def latest_emails(self, limit: int = 20) -> list[dict[str, Any]]:
        inbox_id = self.inbox_mailbox_id()
        calls = [
            [
                "Email/query",
                {
                    "accountId": self.account_id,
                    "filter": {"inMailbox": inbox_id},
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "limit": limit,
                },
                "q0",
            ],
            [
                "Email/get",
                {
                    "accountId": self.account_id,
                    "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                    "properties": [
                        "id",
                        "threadId",
                        "mailboxIds",
                        "keywords",
                        "from",
                        "to",
                        "subject",
                        "receivedAt",
                        "preview",
                        "size",
                    ],
                },
                "g0",
            ],
        ]
        responses = self.jmap(calls)
        for name, payload, _tag in responses:
            if name == "Email/get":
                messages = payload.get("list", []) or []
                return [m for m in messages if isinstance(m, dict)]
        return []

    def event_source_url(self, *, types: str = "Email,Mailbox", closeafter: int = 90, ping: int = 25) -> str:
        session = self.jmap_session(force=True)
        template = session.get("eventSourceUrl")
        if not template:
            raise AtomicMailError("JMAP session has no eventSourceUrl")
        return (
            str(template)
            .replace("{types}", urllib.parse.quote(types))
            .replace("{closeafter}", str(closeafter))
            .replace("{ping}", str(ping))
        )


def inbox_email(inbox_id: str) -> str:
    return inbox_id if "@" in inbox_id else f"{inbox_id}@atomicmail.ai"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_ids": [], "initialized": False, "notifications_sent": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
    except Exception:  # noqa: BLE001
        backup = path.with_suffix(f".corrupt-{int(time.time())}.json")
        path.rename(backup)
        return {"seen_ids": [], "initialized": False, "notifications_sent": 0, "corrupt_backup": str(backup)}
    data.setdefault("seen_ids", [])
    data.setdefault("initialized", False)
    data.setdefault("notifications_sent", 0)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    chmod_best_effort(tmp, 0o600)
    tmp.replace(path)
    chmod_best_effort(path, 0o600)


def person_list(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "unknown sender"
    parts: list[str] = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        email_addr = str(item.get("email") or "")
        if name and email_addr:
            parts.append(f"{name} <{email_addr}>")
        elif email_addr:
            parts.append(email_addr)
        elif name:
            parts.append(name)
    return ", ".join(parts) if parts else "unknown sender"


def format_markdown_notification(messages: list[dict[str, Any]], inbox: str) -> str:
    if not messages:
        return ""
    chunks = [f"## New mail for `{inbox_email(inbox)}`"]
    for msg in messages[:10]:
        subject = str(msg.get("subject") or "(no subject)").strip()
        sender = person_list(msg.get("from"))
        received = str(msg.get("receivedAt") or "")
        preview = " ".join(str(msg.get("preview") or "").split())
        if len(preview) > 400:
            preview = preview[:397] + "..."
        chunks.append(
            "\n".join(
                [
                    f"### {subject}",
                    f"**From:** {sender}",
                    f"**Received:** {received}",
                    f"**Preview:** {preview or '(no preview)'}",
                ]
            )
        )
    if len(messages) > 10:
        chunks.append(f"...and {len(messages) - 10} more new messages.")
    chunks.append("This watcher only notifies; it will not send replies.")
    return "\n\n".join(chunks)


def format_html_notification(messages: list[dict[str, Any]], inbox: str) -> str:
    text = format_markdown_notification(messages, inbox)
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            out.append(f"<b>{html.escape(line[3:])}</b>")
        elif line.startswith("### "):
            out.append(f"\n<b>{html.escape(line[4:])}</b>")
        elif line.startswith("**From:**"):
            out.append("<b>From:</b> " + html.escape(line.split("**From:**", 1)[1].strip()))
        elif line.startswith("**Received:**"):
            out.append("<b>Received:</b> " + html.escape(line.split("**Received:**", 1)[1].strip()))
        elif line.startswith("**Preview:**"):
            out.append("<b>Preview:</b> " + html.escape(line.split("**Preview:**", 1)[1].strip()))
        else:
            out.append(html.escape(line))
    return "\n".join(out)


def send_telegram(message_html: str) -> None:
    token = env_get("TELEGRAM_BOT_TOKEN")
    chat_id = env_get("TELEGRAM_CHAT_ID", "TELEGRAM_HOME_CHANNEL")
    if not token or not chat_id:
        raise AtomicMailError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required for Telegram delivery")
    status, _headers, parsed, raw = http_json(
        "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        body={"chat_id": chat_id, "text": message_html, "parse_mode": "HTML", "disable_web_page_preview": True},
    )
    if status >= 400 or not (isinstance(parsed, dict) and parsed.get("ok")):
        raise AtomicMailError(f"Telegram send failed {status}: {raw[:300]}")


def send_webhook(messages: list[dict[str, Any]], inbox: str) -> None:
    url = env_get("WEBHOOK_URL")
    if not url:
        raise AtomicMailError("WEBHOOK_URL is required for webhook delivery")
    headers = {"Content-Type": "application/json"}
    bearer_token = env_get("WEBHOOK_BEARER_TOKEN")
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    payload = {
        "inbox": inbox_email(inbox),
        "count": len(messages),
        "text": format_markdown_notification(messages, inbox),
        "messages": messages,
    }
    status, _headers, parsed, raw = http_json("POST", url, headers=headers, body=payload)
    if status >= 400:
        raise AtomicMailError(f"webhook send failed {status}: {raw[:300]}")
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        raise AtomicMailError(f"webhook returned ok=false: {raw[:300]}")


def notify(messages: list[dict[str, Any]], inbox: str, args: argparse.Namespace) -> None:
    if not messages:
        return
    if args.emit_stdout:
        print(format_markdown_notification(messages, inbox))
    if args.send_telegram:
        send_telegram(format_html_notification(messages, inbox))
    if args.send_webhook:
        send_webhook(messages, inbox)


def check_new_mail(
    client: AtomicMailClient,
    state_file: Path,
    *,
    initialize_only: bool = False,
    limit: int = 30,
) -> tuple[list[dict[str, Any]], str]:
    state = load_state(state_file)
    emails = client.latest_emails(limit=limit)
    current_ids = [str(m.get("id")) for m in emails if m.get("id")]
    old_seen_ids = [str(x) for x in state.get("seen_ids", [])]
    seen = set(old_seen_ids)

    if not state.get("initialized"):
        state.update(
            {
                "seen_ids": current_ids[:500],
                "initialized": True,
                "initialized_at": utc_now(),
                "last_check_at": utc_now(),
                "inbox": inbox_email(client.config.inbox_id),
            }
        )
        save_state(state_file, state)
        return [], f"initialized {len(current_ids)} existing messages"

    new_messages = [m for m in emails if str(m.get("id")) not in seen]
    new_messages.sort(key=lambda m: str(m.get("receivedAt") or ""))
    if initialize_only:
        state["last_check_at"] = utc_now()
        save_state(state_file, state)
        return [], "already initialized"

    if new_messages:
        merged_seen = current_ids + [msg_id for msg_id in old_seen_ids if msg_id not in set(current_ids)]
        state["seen_ids"] = merged_seen[:500]
        state["last_new_mail_at"] = utc_now()
        state["notifications_sent"] = int(state.get("notifications_sent", 0)) + 1
    state["last_check_at"] = utc_now()
    state["inbox"] = inbox_email(client.config.inbox_id)
    save_state(state_file, state)
    return new_messages, "ok"


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{utc_now()} {message}\n")


def iter_sse_lines(url: str, cap: str) -> Iterable[str]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {cap}", "Accept": "text/event-stream", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=140) as res:
        for raw in res:
            yield raw.decode("utf-8", errors="replace").rstrip("\n")


def run_check(args: argparse.Namespace) -> int:
    config = Config.from_args(args)
    client = AtomicMailClient(config)
    messages, status = check_new_mail(client, config.state_file, initialize_only=args.initialize_only)
    if args.verbose:
        print(json.dumps({"status": status, "new_count": len(messages), **config.public_summary()}, indent=2))
    notify(messages, config.inbox_id, args)
    return 0


def run_watch(args: argparse.Namespace) -> int:
    config = Config.from_args(args)
    client = AtomicMailClient(config)
    backoff = 5
    last_reconcile = 0.0
    messages, status = check_new_mail(client, config.state_file)
    notify(messages, config.inbox_id, args)
    log_line(config.log_file, f"watcher start status={status} inbox={inbox_email(config.inbox_id)}")

    while True:
        try:
            now = time.time()
            if now - last_reconcile > args.reconcile_seconds:
                messages, status = check_new_mail(client, config.state_file)
                notify(messages, config.inbox_id, args)
                log_line(config.log_file, f"reconcile status={status} new={len(messages)}")
                last_reconcile = now

            cap = client.ensure_capability_jwt()
            url = client.event_source_url(types=args.event_types, closeafter=args.closeafter, ping=args.ping)
            log_line(config.log_file, "eventsource connect")
            data_buffer: list[str] = []
            for line in iter_sse_lines(url, cap):
                if line.startswith("data:"):
                    data_buffer.append(line[5:].strip())
                elif line == "":
                    if data_buffer:
                        data = "\n".join(data_buffer).strip()
                        data_buffer = []
                        if data and data != "{}":
                            messages, _status = check_new_mail(client, config.state_file)
                            notify(messages, config.inbox_id, args)
                            log_line(config.log_file, f"event new={len(messages)} data={data[:200]}")
                elif args.verbose:
                    log_line(config.log_file, f"sse {line[:200]}")
            backoff = 5
        except Exception as exc:  # noqa: BLE001
            log_line(config.log_file, f"watcher error {type(exc).__name__}: {exc}")
            if args.verbose:
                traceback.print_exc()
            sleep_for = min(120, backoff + random.random() * 3)
            time.sleep(sleep_for)
            backoff = min(120, backoff * 2)


def run_validate_config(args: argparse.Namespace) -> int:
    config = Config.from_args(args)
    print(json.dumps(config.public_summary(), indent=2))
    return 0


def run_test_notifier(args: argparse.Namespace) -> int:
    inbox = args.test_inbox or env_get("ATOMICMAIL_INBOX_ID") or "test@atomicmail.ai"
    message = [
        {
            "id": "test-message",
            "subject": "Atomic Mail watcher test",
            "from": [{"name": "Atomic Mail Watcher", "email": "watcher@example.invalid"}],
            "receivedAt": utc_now(),
            "preview": "Notifier delivery path is configured correctly.",
        }
    ]
    notify(message, inbox, args)
    print("test notification sent")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Atomic Mail inbox watcher")
    parser.add_argument("--mode", choices=["check", "watch", "validate-config", "test-notifier"], default="check")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--credentials-file", type=Path, default=None)
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--token-dir", type=Path, default=None)
    parser.add_argument("--emit-stdout", action="store_true", help="Print markdown notification to stdout")
    parser.add_argument("--send-telegram", action="store_true", help="Send notification through Telegram Bot API")
    parser.add_argument("--send-webhook", action="store_true", help="POST notification JSON to WEBHOOK_URL")
    parser.add_argument("--initialize-only", action="store_true", help="Seed state with current inbox messages and do not notify")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--event-types", default="Email,Mailbox")
    parser.add_argument("--closeafter", type=int, default=90)
    parser.add_argument("--ping", type=int, default=25)
    parser.add_argument("--reconcile-seconds", type=int, default=3600)
    parser.add_argument("--test-inbox", default=None, help="Inbox label for --mode test-notifier")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "check":
            return run_check(args)
        if args.mode == "watch":
            return run_watch(args)
        if args.mode == "validate-config":
            return run_validate_config(args)
        if args.mode == "test-notifier":
            return run_test_notifier(args)
    except Exception as exc:  # noqa: BLE001
        print(f"atomic-mail-watcher error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
