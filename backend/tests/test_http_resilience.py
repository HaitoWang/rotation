import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from app import http_client
from app import sentinel_quickjs


class HttpResilienceTests(unittest.TestCase):
    def test_explicit_ca_bundle_is_selected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir) / "ca.pem"
            bundle.write_text("test-ca", encoding="ascii")
            with mock.patch.dict(os.environ, {"CURL_CA_BUNDLE": str(bundle)}):
                self.assertEqual(http_client.resolve_ca_bundle(), str(bundle))

    def test_sentinel_request_retries_transient_transport_errors(self):
        response = object()
        session = mock.Mock()
        session.get.side_effect = [RuntimeError("curl 77"), RuntimeError("TLS reset"), response]
        with mock.patch.object(sentinel_quickjs.time, "sleep") as sleep:
            actual = sentinel_quickjs._request_with_retry(
                session, "get", "https://sentinel.example.test", timeout=10
            )

        self.assertIs(actual, response)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_sentinel_cookie_isolation_restores_auth_cookies(self):
        session = SimpleNamespace(cookies={"auth-state": "keep"})

        with sentinel_quickjs._isolated_session_cookies(session):
            session.cookies["sentinel-lb"] = "discard"
            session.cookies["auth-state"] = "polluted"

        self.assertEqual(session.cookies, {"auth-state": "keep"})

    def test_sentinel_cookie_isolation_restores_curl_cookie_jar(self):
        session = http_client.create_http_session()
        if not hasattr(session.cookies, "jar"):
            self.skipTest("HTTP backend does not expose a cookie jar")
        session.cookies.set("auth-state", "keep", domain="auth.openai.com")

        with sentinel_quickjs._isolated_session_cookies(session):
            session.cookies.set("sentinel-lb", "discard", domain="sentinel.openai.com")

        actual = [(c.name, c.value, c.domain) for c in session.cookies.jar]
        self.assertEqual(actual, [("auth-state", "keep", "auth.openai.com")])

    def test_sentinel_cookie_isolation_does_not_swallow_errors(self):
        session = SimpleNamespace(cookies={"auth-state": "keep"})

        with self.assertRaisesRegex(RuntimeError, "sentinel failed"):
            with sentinel_quickjs._isolated_session_cookies(session):
                raise RuntimeError("sentinel failed")


if __name__ == "__main__":
    unittest.main()
