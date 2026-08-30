import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.integrations.openai.auth_flow import AuthFlow


class ExistingPasswordResolutionTests(unittest.TestCase):
    def test_codex_login_fallback_refuses_to_guess_password(self):
        flow = object.__new__(AuthFlow)
        flow.result = type("Result", (), {
            "email": "existing@example.com",
            "password": "",
        })()
        flow._env_overrides = {}
        flow._account_callback = lambda _email: {}

        with self.assertRaisesRegex(RuntimeError, "existing_account_missing_password"):
            flow._codex_drive_login_from_log_in()

    def test_protocol_login_refuses_to_guess_password_before_network(self):
        flow = object.__new__(AuthFlow)
        flow.result = type("Result", (), {
            "email": "",
            "password": "",
        })()
        flow._env_overrides = {}
        flow._account_callback = lambda _email: {}
        flow._is_existing_account = False
        flow.check_proxy = lambda: True
        flow.warmup = lambda: True

        with self.assertRaisesRegex(RuntimeError, "existing_account_missing_password"):
            flow.run_protocol_login(None, "existing@example.com")


if __name__ == "__main__":
    unittest.main()
