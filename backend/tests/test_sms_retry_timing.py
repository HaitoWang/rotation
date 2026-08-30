import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.integrations.sms.provider import _earliest_retry_after


class SmsRetryTimingTests(unittest.TestCase):
    def test_uses_earliest_candidate_instead_of_longest_cooldown(self):
        wait = 0.0
        for candidate in (300.0, 29.5, 1.0):
            wait = _earliest_retry_after(wait, candidate)
        self.assertEqual(wait, 1.0)

    def test_ignores_non_positive_candidates(self):
        self.assertEqual(_earliest_retry_after(12.0, 0.0), 12.0)


if __name__ == "__main__":
    unittest.main()
