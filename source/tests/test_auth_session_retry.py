import unittest
from unittest import mock

from auth_flow import AuthFlow


class AuthSessionRetryTests(unittest.TestCase):
    def test_invalid_state_rebuilds_entire_bootstrap(self):
        flow = object.__new__(AuthFlow)
        flow._get_env = mock.Mock(return_value="3")
        flow.get_csrf_token = mock.Mock(side_effect=[
            RuntimeError("HTTP 409 invalid_state: session is no longer valid"),
            "csrf-2",
        ])
        flow.get_auth_url = mock.Mock(return_value="https://auth.example/oauth")
        flow.auth_oauth_init = mock.Mock(return_value="device-2")
        flow.get_sentinel_token = mock.Mock(return_value="sentinel-2")
        flow.signup = mock.Mock(return_value=True)
        flow._restart_auth_bootstrap_session = mock.Mock()
        flow.check_proxy = mock.Mock(return_value=True)
        flow.warmup = mock.Mock(return_value=True)

        auth_url, is_new = flow._bootstrap_signup("person@outlook.com")

        self.assertEqual(auth_url, "https://auth.example/oauth")
        self.assertTrue(is_new)
        self.assertEqual(flow.get_csrf_token.call_count, 2)
        flow._restart_auth_bootstrap_session.assert_called_once_with("person@outlook.com")
        flow.get_auth_url.assert_called_once_with("csrf-2", email="person@outlook.com")

    def test_non_session_error_is_not_replayed_on_same_proxy(self):
        flow = object.__new__(AuthFlow)
        flow._get_env = mock.Mock(return_value="3")
        flow.get_csrf_token = mock.Mock(side_effect=RuntimeError("proxy connect timeout"))
        flow._restart_auth_bootstrap_session = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "proxy connect timeout"):
            flow._bootstrap_signup("person@outlook.com")

        flow._restart_auth_bootstrap_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
