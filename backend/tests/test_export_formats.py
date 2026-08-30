import unittest
from app.services import export_formats


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


if __name__ == "__main__":
    unittest.main()
