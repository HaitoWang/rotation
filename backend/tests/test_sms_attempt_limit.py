import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth_flow import AuthFlow
from fastapi import HTTPException
from app.webui.app import SaveSmsConfigReq, api_save_sms_config
from app.webui.registrar import classify_error


class _Controller:
    provider_key = "smsbower"

    def __init__(self, limit):
        self.config = {"sms_max_phone_attempts": str(limit), "sms_per_phone_timeout": "40"}
        self.get_phone_calls = 0
        self.send_failures = 0

    def get_phone(self):
        self.get_phone_calls += 1
        return f"+1000000000{self.get_phone_calls}"

    def mark_send_failed(self, _reason):
        self.send_failures += 1


class SmsAttemptLimitTests(unittest.TestCase):
    def test_sms_settings_reject_non_positive_attempt_limit(self):
        with mock.patch("app.webui.app.db.save_sms_config") as save:
            with self.assertRaises(HTTPException) as raised:
                api_save_sms_config(SaveSmsConfigReq(sms_max_phone_attempts="0"))
        self.assertEqual(raised.exception.status_code, 400)
        save.assert_not_called()

    def test_attempt_limit_is_retryable_without_immediate_proxy_retry(self):
        self.assertEqual(
            classify_error("SMS 接码达到最多手机号尝试次数 3，仍未完成", "outlook"),
            "sms_exhausted",
        )

    @mock.patch("auth_flow.time.sleep", return_value=None)
    def test_sms_loop_stops_at_configured_phone_limit(self, _sleep):
        flow = AuthFlow.__new__(AuthFlow)
        flow._should_stop = None
        flow._add_phone_send = mock.Mock(side_effect=RuntimeError("invalid_phone_number"))
        controller = _Controller(3)

        with self.assertRaisesRegex(RuntimeError, "最多手机号尝试次数 3"):
            flow._do_sms_loop(controller)

        self.assertEqual(controller.get_phone_calls, 3)
        self.assertEqual(controller.send_failures, 3)


if __name__ == "__main__":
    unittest.main()
