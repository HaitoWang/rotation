import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from webui import db, export_formats


class ExportFormatsTest(unittest.TestCase):
    def test_team_sso_free_account_format_is_registered(self):
        fmt = export_formats.get_format("team_sso_free_account")

        self.assertIsNotNone(fmt)
        self.assertEqual(
            fmt.label,
            "邮箱----密码(gpt)----client_id----refresh_token----2FA----access_token(gpt)----refresh_token(gpt)",
        )
        self.assertEqual(fmt.filename, "team-sso-free账号.txt")

    def test_team_sso_free_account_format_preserves_empty_fields(self):
        rows = [
            {
                "email": "person@outlook.com",
                "password": "gpt-password",
                "mail_client_id": "mail-client",
                "mail_refresh_token": "mail-refresh",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "access_token": "gpt-web-at",
                "refresh_token": "gpt-codex-rt",
            },
            {"email": "incomplete@outlook.com"},
        ]

        self.assertEqual(
            export_formats.render_text(rows, "team_sso_free_account"),
            "person@outlook.com----gpt-password----mail-client----mail-refresh----"
            "JBSWY3DPEHPK3PXP----gpt-web-at----gpt-codex-rt\n"
            "incomplete@outlook.com------------------------",
        )

    def test_team_sso_no_2fa_format_uses_six_fields(self):
        row = {
            "email": "person@outlook.com",
            "password": "gpt-password",
            "mail_client_id": "mail-client",
            "mail_refresh_token": "mail-refresh",
            "access_token": "gpt-web-at",
            "refresh_token": "gpt-codex-rt",
        }

        self.assertEqual(
            export_formats.render_text([row], "team_sso_free_account_no_2fa"),
            "person@outlook.com----gpt-password----mail-client----mail-refresh----"
            "gpt-web-at----gpt-codex-rt",
        )

    def test_registered_export_rows_include_outlook_mail_credentials(self):
        original_path = db.DB_PATH
        try:
            with TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "webui.db"
                db.init_db()
                con = db._conn()
                con.execute(
                    "INSERT INTO outlook_accounts "
                    "(email, password, client_id, refresh_token, status) "
                    "VALUES (?, ?, ?, ?, 'done')",
                    ("person@outlook.com", "mail-password", "mail-client", "mail-refresh"),
                )
                con.execute(
                    "INSERT INTO registered "
                    "(email, password, access_token, refresh_token, totp_secret, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (
                        "person@outlook.com",
                        "gpt-password",
                        "gpt-web-at",
                        "gpt-codex-rt",
                        "JBSWY3DPEHPK3PXP",
                    ),
                )
                con.commit()
                con.close()

                row = db.list_registered_full()[0]
                self.assertEqual(row["password"], "gpt-password")
                self.assertEqual(row["mail_client_id"], "mail-client")
                self.assertEqual(row["mail_refresh_token"], "mail-refresh")
        finally:
            db.DB_PATH = original_path

    def test_registered_export_filter_and_soft_delete(self):
        original_path = db.DB_PATH
        try:
            with TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "webui.db"
                db.init_db()
                db.save_registered({
                    "email": "has-rt@example.com",
                    "access_token": "at",
                    "refresh_token": "rt",
                    "created_at": 2,
                })
                db.save_registered({
                    "email": "no-rt@example.com",
                    "access_token": "at",
                    "refresh_token": "",
                    "created_at": 1,
                })

                rows = db.list_registered_full(limit=100, filter_rt="has_rt")
                self.assertEqual([row["email"] for row in rows], ["has-rt@example.com"])
                self.assertEqual(db.soft_delete_registered_by_emails([rows[0]["email"]]), 1)
                self.assertEqual(db.count_registered("has_rt"), 0)
                self.assertIsNone(db.get_registered("has-rt@example.com"))
                self.assertEqual(
                    [row["email"] for row in db.list_registered_full(filter_rt="all")],
                    ["no-rt@example.com"],
                )
        finally:
            db.DB_PATH = original_path

    def test_registered_email_scan_and_delete_banned_only(self):
        original_path = db.DB_PATH
        try:
            with TemporaryDirectory() as tmp:
                db.DB_PATH = Path(tmp) / "webui.db"
                db.init_db()
                db.save_registered({
                    "email": "banned@example.com",
                    "access_token": "banned-at",
                })
                db.save_registered({
                    "email": "free@example.com",
                    "access_token": "free-at",
                })
                db.save_registered({
                    "email": "unchecked@example.com",
                    "access_token": "unchecked-at",
                })
                db.update_plus_check(
                    "banned@example.com", {"status": "banned", "label": "封号"}
                )
                db.update_plus_check(
                    "free@example.com", {"status": "free", "label": "Free"}
                )

                self.assertEqual(
                    set(db.list_registered_emails()),
                    {"banned@example.com", "free@example.com", "unchecked@example.com"},
                )
                self.assertEqual(db.delete_banned_registered(), 1)
                self.assertIsNone(db.get_registered("banned@example.com"))
                self.assertIsNotNone(db.get_registered("free@example.com"))
                self.assertIsNotNone(db.get_registered("unchecked@example.com"))
                self.assertEqual(db.delete_banned_registered(), 0)
        finally:
            db.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
