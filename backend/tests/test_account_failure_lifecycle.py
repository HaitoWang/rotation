import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.webui import db
from app.webui.registrar import classify_error


class AccountFailureClassificationTests(unittest.TestCase):
    def test_non_reusable_account_errors_are_classified_as_account(self):
        errors = (
            "Codex OAuth 未获取 refresh_token，本次不计成功: "
            "mfa_challenge_missing_totp_secret",
            "密码登录失败: 403 - account has been deleted or deactivated",
            "密码登录失败: 401 - code=invalid_username_or_password",
            "IMAP 登录失败: b'AUTHENTICATE failed.'",
            "existing_account_missing_password",
            "totp_activated_but_persistence_failed",
        )
        for error in errors:
            with self.subTest(error=error):
                self.assertEqual(classify_error(error, "outlook"), "account")


class FailedAccountClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.path_patch = mock.patch.object(db, "DB_PATH", self.db_path)
        self.path_patch.start()
        db.init_db()
        # init_db closes the thread-local handle; force the next DB helper to
        # create a live connection for this temporary database.
        db._db_local.connection = None
        db._db_local.path = ""
        con = db._conn()
        con.execute(
            "INSERT INTO outlook_accounts "
            "(email, password, client_id, refresh_token, relay_url, kind, status, imported_at) "
            "VALUES (?, '', '', '', '', 'outlook', 'available', 1)",
            ("failed@example.com",),
        )
        con.commit()

    def tearDown(self):
        con = getattr(db._db_local, "connection", None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        db._db_local.connection = None
        db._db_local.path = ""
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_failed_account_requires_explicit_reset_before_claim(self):
        db.mark_failed("failed@example.com", "[account] invalid_username_or_password")

        self.assertIsNone(db.claim_account("failed@example.com"))
        self.assertEqual(db.get_account("failed@example.com")["status"], "failed")

        self.assertTrue(db.reset_to_available("failed@example.com"))
        claimed = db.claim_account("failed@example.com")
        self.assertIsNotNone(claimed)
        self.assertEqual(db.get_account("failed@example.com")["status"], "in_use")

    def test_network_release_remains_claimable(self):
        claimed = db.claim_account("failed@example.com")
        self.assertIsNotNone(claimed)

        db.release_unused("failed@example.com")

        self.assertIsNotNone(db.claim_account("failed@example.com"))

    def test_early_totp_merge_preserves_password_and_tokens(self):
        db.save_password_early("failed@example.com", "KnownPassword-123")
        con = db._conn()
        con.execute(
            "UPDATE registered SET refresh_token='existing-rt' WHERE email=?",
            ("failed@example.com",),
        )
        con.commit()

        db.save_totp_early(
            "failed@example.com",
            "JBSWY3DPEHPK3PXPJBSWY3DP",
            "factor-1",
        )

        row = db.get_registered("failed@example.com")
        self.assertEqual(row["password"], "KnownPassword-123")
        self.assertEqual(row["refresh_token"], "existing-rt")
        self.assertEqual(row["totp_secret"], "JBSWY3DPEHPK3PXPJBSWY3DP")
        self.assertEqual(row["totp_factor_id"], "factor-1")

    def test_bulk_reset_skips_terminal_failures(self):
        con = db._conn()
        con.executemany(
            "INSERT INTO outlook_accounts "
            "(email, password, client_id, refresh_token, relay_url, kind, status, imported_at, fail_reason) "
            "VALUES (?, '', '', '', '', 'outlook', 'failed', 1, ?)",
            [
                ("terminal@example.com", "[account] invalid_username_or_password"),
                ("retryable@example.com", "[unknown] temporary provider response"),
            ],
        )
        con.commit()

        self.assertEqual(db.reset_failed_to_available(), 1)
        self.assertEqual(db.get_account("terminal@example.com")["status"], "failed")
        self.assertEqual(db.get_account("retryable@example.com")["status"], "available")


if __name__ == "__main__":
    unittest.main()
