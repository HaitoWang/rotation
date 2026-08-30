import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.integrations.openai.auth_flow import AuthFlow


class CodexAddPhoneCallbackTests(unittest.TestCase):
    def _flow(self):
        flow = object.__new__(AuthFlow)
        flow._codex_rt_attempted = False
        flow._sms_callback = object()
        flow._oauth_auth_url = ""
        flow._oauth_client_id = ""
        flow._oauth_redirect_uri = ""
        flow._oauth_state = ""
        flow._manual_login_verifier = ""
        flow._captured_login_verifier = ""
        flow._env_flag = mock.Mock(return_value=False)
        flow._build_codex_authorize = mock.Mock(return_value=(
            "https://auth.openai.com/oauth/authorize?prompt=login",
            "expected-state",
            "verifier",
            "http://localhost:1455/auth/callback",
            "client-id",
        ))
        flow._follow_authorize_for_callback = mock.Mock(return_value=(
            "",
            "https://auth.openai.com/add-phone",
        ))
        flow._handle_add_phone_via_sms = mock.Mock(return_value=(
            "http://localhost:1455/auth/callback?code=one-time-code&state=expected-state"
        ))
        flow._exchange_codex_callback_code = mock.Mock(return_value=True)
        return flow

    def test_direct_add_phone_uses_callback_returned_by_phone_validation(self):
        flow = self._flow()

        ok = flow.oauth_codex_rt_exchange()

        self.assertTrue(ok)
        flow._handle_add_phone_via_sms.assert_called_once_with(
            continue_url="https://auth.openai.com/add-phone"
        )
        flow._exchange_codex_callback_code.assert_called_once_with(
            callback_url=(
                "http://localhost:1455/auth/callback?"
                "code=one-time-code&state=expected-state"
            ),
            expected_state="expected-state",
            verifier="verifier",
            redirect_uri="http://localhost:1455/auth/callback",
            client_id="client-id",
        )
        self.assertEqual(flow._follow_authorize_for_callback.call_count, 1)


if __name__ == "__main__":
    unittest.main()
