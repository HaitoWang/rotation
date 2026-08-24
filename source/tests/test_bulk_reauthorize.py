import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webui.app import (
    BulkReauthorizeRegisteredReq,
    api_bulk_reauthorize_registered,
)


class BulkReauthorizeTests(unittest.TestCase):
    def test_bulk_reauthorize_deduplicates_and_reports_each_failure(self):
        credentials = {
            "first@example.com": {"email": "first@example.com"},
            "second@example.com": {"email": "second@example.com"},
        }

        def reauthorize(email, **_kwargs):
            if email == "second@example.com":
                return {"ok": False, "error": "SMS timeout", "run_id": "run-2"}
            return {
                "ok": True,
                "run_id": "run-1",
                "account": {
                    "access_token": "access",
                    "session_token": "session",
                    "refresh_token": "refresh",
                },
            }

        with mock.patch(
            "webui.app.db.get_registered",
            side_effect=lambda email: credentials.get(email),
        ), mock.patch(
            "webui.app.registrar.reauthorize_registered_account",
            side_effect=reauthorize,
        ) as run, mock.patch(
            "webui.app.db.release_team_rotation_auth_required",
            side_effect=lambda email: email == "first@example.com",
        ) as release, mock.patch(
            "webui.app.TEAM_ROTATION.notify_candidate_available",
        ) as notify:
            result = api_bulk_reauthorize_registered(
                BulkReauthorizeRegisteredReq(
                    emails=[
                        "FIRST@example.com",
                        "first@example.com",
                        "missing@example.com",
                        "second@example.com",
                    ],
                    proxy="socks5://proxy",
                    concurrency=2,
                )
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(
            [item["email"] for item in result["results"]],
            ["first@example.com", "missing@example.com", "second@example.com"],
        )
        self.assertEqual(result["results"][1]["error"], "账号池中未找到账号: missing@example.com")
        self.assertEqual(result["results"][2]["error"], "SMS timeout")
        self.assertEqual(run.call_count, 2)
        run.assert_any_call("first@example.com", proxy="socks5://proxy")
        run.assert_any_call("second@example.com", proxy="socks5://proxy")
        release.assert_called_once_with("first@example.com")
        notify.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
