import unittest

from app.mail_providers import create_mail_provider, list_pooled_providers, parse_import_text
from app.mail_providers.mailbox_url import MailboxURLProvider


class _Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = payload if isinstance(payload, str) else ""

    def json(self):
        if isinstance(self.payload, str):
            raise ValueError("not json")
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.trust_env = True
        self.closed = False

    def get(self, _url, **_kwargs):
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def close(self):
        self.closed = True


class _Clock:
    def __init__(self):
        self.monotonic_value = 0.0
        self.wall_value = 2_000_000_000.0

    def monotonic(self):
        return self.monotonic_value

    def wall(self):
        return self.wall_value

    def sleep(self, seconds):
        step = max(float(seconds), 0.1)
        self.monotonic_value += step
        self.wall_value += step


class MailboxURLProviderTests(unittest.TestCase):
    def test_provider_is_listed_and_parses_email_url_rows(self):
        providers = {item["kind"]: item for item in list_pooled_providers()}
        self.assertIn("mailbox_url", providers)
        self.assertEqual(providers["mailbox_url"]["line_segments"], 2)

        rows = parse_import_text(
            "User@Example.com----https://assurivo.com/console/open.php?mail=user",
            "mailbox_url",
        )
        self.assertEqual(rows, [{
            "email": "user@example.com",
            "relay_url": "https://assurivo.com/console/open.php?mail=user",
            "kind": "mailbox_url",
        }])

    def test_from_config_rejects_hosts_outside_allowlist(self):
        with self.assertRaisesRegex(Exception, "接码地址域名必须是"):
            create_mail_provider(
                "mailbox_url",
                {"mailbox_url_allowed_hosts": "assurivo.com"},
                {
                    "email": "user@example.com",
                    "relay_url": "https://127.0.0.1/mailbox",
                },
            )

    def test_snapshot_filters_old_code_and_confirms_new_code_twice(self):
        clock = _Clock()
        session = _Session([
            _Response({"message": "old code 111111"}),
            _Response({"message": "new code 222222"}),
            _Response({"message": "new code 222222"}),
        ])
        provider = MailboxURLProvider(
            "user@example.com",
            "https://assurivo.com/mailbox",
            timeout=10,
            poll_interval=0.1,
            session=session,
            clock=clock.monotonic,
            wall_clock=clock.wall,
            sleeper=clock.sleep,
        )

        self.assertEqual(provider.create_mailbox(), "user@example.com")
        self.assertEqual(provider.wait_for_otp("user@example.com", timeout=10), "222222")

    def test_unchanged_snapshot_code_times_out(self):
        clock = _Clock()
        session = _Session([_Response({"code": "111111"})])
        provider = MailboxURLProvider(
            "user@example.com",
            "https://assurivo.com/mailbox",
            timeout=10,
            poll_interval=1,
            session=session,
            clock=clock.monotonic,
            wall_clock=clock.wall,
            sleeper=clock.sleep,
        )

        provider.create_mailbox()
        with self.assertRaisesRegex(TimeoutError, "OTP 超时"):
            provider.wait_for_otp("user@example.com", timeout=1)

    def test_issued_after_filters_stale_provider_messages(self):
        clock = _Clock()
        session = _Session([
            _Response({"message": "waiting"}),
            _Response({"code": "222222", "received_at": 1_999_999_000}),
            _Response({"code": "333333", "received_at": 2_000_000_010}),
            _Response({"code": "333333", "received_at": 2_000_000_010}),
        ])
        provider = MailboxURLProvider(
            "user@example.com",
            "https://assurivo.com/mailbox",
            timeout=10,
            poll_interval=0.1,
            session=session,
            clock=clock.monotonic,
            wall_clock=clock.wall,
            sleeper=clock.sleep,
        )

        provider.create_mailbox()
        self.assertEqual(
            provider.wait_for_otp(
                "user@example.com", timeout=10, issued_after=2_000_000_000
            ),
            "333333",
        )

    def test_redirect_must_stay_on_allowed_host(self):
        session = _Session([
            _Response("", status=302, headers={"Location": "http://127.0.0.1/private"})
        ])
        provider = MailboxURLProvider(
            "user@example.com",
            "https://assurivo.com/mailbox",
            session=session,
        )
        with self.assertRaisesRegex(Exception, "接码地址域名必须是"):
            provider.create_mailbox()


if __name__ == "__main__":
    unittest.main()
