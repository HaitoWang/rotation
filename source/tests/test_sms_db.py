import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from webui import db
from webui import sms_cancel_dispatcher


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

    def test_sms_cleanup_queue_claims_failed_and_stale_rentals(self):
        now = time.time()
        db.track_sms_activation(
            "herosms",
            "failed-1",
            phone_number="+661",
            acquired_at=now - 30,
        )
        db.queue_sms_activation_cancel(
            "herosms",
            "failed-1",
            acquired_at=now - 30,
            not_before=now - 1,
            error="cancel denied",
        )
        db.track_sms_activation(
            "herosms",
            "stale-1",
            phone_number="+662",
            acquired_at=now - 1300,
            lifetime_seconds=1200,
        )

        claimed = db.claim_sms_activation_cancellations(limit=10)

        self.assertEqual(
            {(row["platform"], row["activation_id"]) for row in claimed},
            {("herosms", "failed-1"), ("herosms", "stale-1")},
        )

    def test_sms_cleanup_dispatcher_retries_then_completes(self):
        now = time.time()
        db.save_sms_config({
            "sms_provider": "herosms",
            "sms_herosms_api_key": "hero-secret",
        })
        db.queue_sms_activation_cancel(
            "herosms",
            "retry-1",
            acquired_at=now - 180,
            not_before=now - 1,
            error="initial",
        )
        provider = mock.Mock()
        provider.cancel.side_effect = [False, True]
        with mock.patch.object(
            sms_cancel_dispatcher, "create_sms_provider", return_value=provider
        ):
            self.assertEqual(sms_cancel_dispatcher.process_once(), 1)
            con = db._conn()
            con.execute(
                "UPDATE sms_activation_cleanup SET next_attempt_at=0, lease_until=0 "
                "WHERE platform='herosms' AND activation_id='retry-1'"
            )
            con.commit()
            con.close()
            self.assertEqual(sms_cancel_dispatcher.process_once(), 1)

        self.assertEqual(db.sms_activation_cleanup_pending_count(), 0)
        self.assertEqual(provider.cancel.call_count, 2)


if __name__ == "__main__":
    unittest.main()
