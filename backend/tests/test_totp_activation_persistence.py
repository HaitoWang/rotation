import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.webui import two_factor


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self, verification_error=None):
        self.posts = 0
        self.gets = 0
        self.verification_error = verification_error

    def get(self, *_args, **_kwargs):
        self.gets += 1
        if self.gets == 1:
            return _Response(200, {"mfa_enabled": False})
        if self.verification_error:
            raise self.verification_error
        return _Response(200, {"mfa_enabled": True})

    def post(self, url, *_args, **_kwargs):
        self.posts += 1
        if url.endswith("/mfa/enroll"):
            return _Response(200, {
                "secret": "JBSWY3DPEHPK3PXPJBSWY3DP",
                "session_id": "session-1",
                "factor": {"id": "factor-1"},
            })
        return _Response(200, {"success": True})


class _Flow:
    def __init__(self, verification_error=None):
        self.session = _Session(verification_error)

    @staticmethod
    def _common_headers(_referer):
        return {}


class TotpActivationPersistenceTests(unittest.TestCase):
    @mock.patch.object(two_factor.time, "sleep", return_value=None)
    def test_secret_is_persisted_before_verification_get(self, _sleep):
        calls = []
        flow = _Flow(RuntimeError("verification transport failed"))

        result = two_factor._enroll_and_activate(
            flow,
            "access-token",
            on_activated=lambda secret, factor: calls.append((secret, factor)),
        )

        self.assertEqual(calls, [("JBSWY3DPEHPK3PXPJBSWY3DP", "factor-1")])
        self.assertEqual(result["secret"], "JBSWY3DPEHPK3PXPJBSWY3DP")

    def test_persistence_failure_propagates_after_activate(self):
        flow = _Flow()

        with self.assertRaises(two_factor.ActivationPersistenceError):
            two_factor._enroll_and_activate(
                flow,
                "access-token",
                on_activated=lambda _secret, _factor: (_ for _ in ()).throw(
                    OSError("disk full")
                ),
            )

        self.assertEqual(flow.session.posts, 2)
        self.assertEqual(flow.session.gets, 1)


if __name__ == "__main__":
    unittest.main()
