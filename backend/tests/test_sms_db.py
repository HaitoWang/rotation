import tempfile
import unittest
from pathlib import Path

from app.webui import db


class SmsConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "webui.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_legacy_key_is_exposed_as_masked_platform_config(self):
        db.set_setting("sms_provider", "smsbower")
        db.set_setting("sms_api_key", "legacy-secret")

        public = db.get_sms_config()
        internal = db.get_sms_internal_config()

        self.assertEqual(public["sms_smsbower_api_key"], "***")
        self.assertEqual(public["sms_smsbower_enabled"], "1")
        self.assertEqual(internal["sms_smsbower_api_key"], "legacy-secret")

    def test_dual_platform_keys_and_mode_round_trip(self):
        db.save_sms_config({
            "sms_mode": "race",
            "sms_supplier_strategy": "success_first",
            "sms_smsbower_enabled": "1",
            "sms_smsbower_api_key": "bower-secret",
            "sms_herosms_enabled": "1",
            "sms_herosms_api_key": "hero-secret",
        })

        internal = db.get_sms_internal_config()
        self.assertEqual(internal["sms_mode"], "race")
        self.assertEqual(internal["sms_supplier_strategy"], "success_first")
        self.assertTrue(internal["sms_smsbower_enabled"])
        self.assertTrue(internal["sms_herosms_enabled"])
        self.assertEqual(internal["sms_smsbower_api_key"], "bower-secret")
        self.assertEqual(internal["sms_herosms_api_key"], "hero-secret")

    def test_empty_refresh_token_does_not_overwrite_existing_token(self):
        db.save_registered({
            "email": "keep@example.com",
            "password": "pw",
            "access_token": "at-old",
            "session_token": "st-old",
            "refresh_token": "rt-old",
        })
        db.save_registered({
            "email": "keep@example.com",
            "access_token": "at-new",
            "session_token": "st-new",
            "refresh_token": "",
        })
        saved = db.get_registered("keep@example.com")
        self.assertEqual(saved["refresh_token"], "rt-old")
        self.assertEqual(saved["access_token"], "at-new")


if __name__ == "__main__":
    unittest.main()
