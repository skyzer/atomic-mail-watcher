import os
import tempfile
import unittest
from pathlib import Path

from atomicmail_watcher import Config, check_new_mail, format_html_notification, format_markdown_notification, inbox_email


class FakeClient:
    def __init__(self, state):
        self.config = type("Config", (), {"inbox_id": "agent@atomicmail.ai"})()
        self.state = state

    def latest_emails(self, limit=30):
        return list(self.state)


class AtomicMailWatcherTests(unittest.TestCase):
    def test_inbox_email_adds_domain(self):
        self.assertEqual(inbox_email("agent"), "agent@atomicmail.ai")
        self.assertEqual(inbox_email("agent@atomicmail.ai"), "agent@atomicmail.ai")

    def test_notification_formatting(self):
        messages = [
            {
                "id": "m1",
                "subject": "Hello <world>",
                "from": [{"name": "Ada", "email": "ada@example.com"}],
                "receivedAt": "2026-01-01T00:00:00Z",
                "preview": "Line one\nLine two",
            }
        ]
        md = format_markdown_notification(messages, "agent@atomicmail.ai")
        self.assertIn("New mail", md)
        self.assertIn("Hello <world>", md)
        self.assertIn("Ada <ada@example.com>", md)
        html = format_html_notification(messages, "agent@atomicmail.ai")
        self.assertIn("Hello &lt;world&gt;", html)

    def test_check_new_mail_initializes_then_detects_new(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            messages = [
                {"id": "old", "subject": "Old", "receivedAt": "2026-01-01T00:00:00Z"},
            ]
            client = FakeClient(messages)
            new, status = check_new_mail(client, state_file)
            self.assertEqual(new, [])
            self.assertIn("initialized", status)

            messages.insert(0, {"id": "new", "subject": "New", "receivedAt": "2026-01-02T00:00:00Z"})
            new, status = check_new_mail(client, state_file)
            self.assertEqual(status, "ok")
            self.assertEqual([m["id"] for m in new], ["new"])

            new, status = check_new_mail(client, state_file)
            self.assertEqual(new, [])

    def test_config_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            creds = data / "credentials.json"
            creds.write_text('{"inboxId":"file@atomicmail.ai","apiKey":"file-key"}', encoding="utf-8")
            old_env = dict(os.environ)
            try:
                os.environ["ATOMICMAIL_INBOX_ID"] = "env@atomicmail.ai"
                os.environ["ATOMICMAIL_API_KEY"] = "env-key"
                args = type(
                    "Args",
                    (), {
                        "data_dir": data,
                        "credentials_file": creds,
                        "state_file": None,
                        "log_file": None,
                        "token_dir": None,
                    },
                )()
                cfg = Config.from_args(args)
                self.assertEqual(cfg.inbox_id, "env@atomicmail.ai")
                self.assertEqual(cfg.api_key, "env-key")
            finally:
                os.environ.clear()
                os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
