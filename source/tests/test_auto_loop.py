import sys
import unittest
import queue
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webui.auto_loop import (
    AutoLoopController,
    _parse_proxy_pool,
    _phase_from_event,
    _proxy_session_key,
)
from webui import registrar
from sms_provider import _rotate_country_ids


class AutoLoopProxyTests(unittest.TestCase):
    def test_proxy_pool_deduplicates_session_ids(self):
        first = "socks5://user-session-abc-sessTime-5:pw@proxy.test:10000"
        duplicate = "socks5://other-session-abc-sessTime-10:pw@proxy.test:10000"
        second = "socks5://user-session-def-sessTime-5:pw@proxy.test:10000"
        self.assertEqual(_parse_proxy_pool("\n".join((first, duplicate, second))), [first, second])
        self.assertEqual(_proxy_session_key(first), _proxy_session_key(duplicate))

    def test_start_rejects_insufficient_unique_sessions(self):
        controller = AutoLoopController()
        result = controller.start({
            "concurrency": 2,
            "proxy_pool": "socks5://user-session-one:pw@proxy.test:10000\n"
                          "socks5://other-session-one:pw@proxy.test:10000",
        })
        self.assertFalse(result["ok"])
        self.assertIn("去重后仅 1 个", result["error"])

    def test_retry_rotation_keeps_sessions_exclusive(self):
        controller = AutoLoopController()
        controller._proxy_pool = _parse_proxy_pool("\n".join(
            f"socks5://user-session-{value}:pw@proxy.test:10000"
            for value in ("one", "two", "three")
        ))
        first = controller._proxy_for_worker(0)
        second = controller._proxy_for_worker(1)
        rotated = controller._proxy_for_worker(0, 1)
        self.assertNotEqual(_proxy_session_key(first), _proxy_session_key(rotated))
        self.assertNotEqual(_proxy_session_key(second), _proxy_session_key(rotated))

    def test_phase_detection_tracks_sms_and_codex(self):
        self.assertEqual(_phase_from_event("phase", {"phase": "sms"}, "注册流程"), "短信验证")
        self.assertEqual(
            _phase_from_event("phase", {"phase": "sms_queue"}, "短信验证"), "接码排队"
        )
        self.assertEqual(
            _phase_from_event(
                "log", {"message": "[sms] 接码资源暂不可用，29.4s 后重试"}, "注册流程"
            ),
            "接码排队",
        )
        self.assertEqual(
            _phase_from_event("log", {"message": "尝试 Codex OAuth 直连换取 refresh_token"}, "短信验证"),
            "Codex OAuth",
        )

    def test_log_stream_disconnect_does_not_remove_auto_loop_observer(self):
        run_id = "observer-lifecycle-test"
        observer = object()
        with registrar._lock:
            registrar._run_queues[run_id] = queue.Queue()
            registrar._run_observers[run_id] = observer
        try:
            registrar.remove_run_queue(run_id)
            self.assertIs(registrar._run_observers.get(run_id), observer)
        finally:
            registrar.remove_run_observer(run_id)

    def test_snapshot_reports_effective_concurrency_and_stuck_tasks(self):
        controller = AutoLoopController()
        controller._state = "running"
        controller._started_at = 1000.0
        controller._concurrency = 2
        controller._worker_status = {
            0: {"run_id": "a", "proxy": "socks5://u-session-one:p@proxy:1", "started_at": 900.0, "phase": "注册流程"},
            1: {"run_id": "b", "proxy": "socks5://u-session-two:p@proxy:1", "started_at": -1000.0, "last_activity_at": -1000.0, "phase": "短信验证"},
        }
        with mock.patch("webui.auto_loop.time.time", return_value=1100.0), \
                mock.patch("webui.auto_loop.db.stats", return_value={}):
            snapshot = controller.status()
        self.assertEqual(snapshot["effective_concurrency"], 1)
        self.assertEqual(snapshot["stuck_task_count"], 1)
        self.assertEqual(snapshot["independent_proxy_count"], 2)
        self.assertEqual(snapshot["stage_counts"], {"注册流程": 1, "短信验证": 1})

    def test_country_rotation_spreads_equal_quality_candidates(self):
        first = _rotate_country_ids("smsbower-test", ["52", "6", "73"])
        second = _rotate_country_ids("smsbower-test", ["52", "6", "73"])
        self.assertNotEqual(first[0], second[0])
        self.assertEqual(set(first), {"52", "6", "73"})

    def test_wait_run_finish_marks_timeout_as_stuck(self):
        controller = AutoLoopController()
        with mock.patch.object(registrar, "wait_run_done", return_value=False), \
                mock.patch.object(controller, "_stuck_cancel_grace_seconds", 0):
            controller._stuck_cancel_grace_seconds = 1
            result = controller._wait_run_finish("stuck-run", timeout=1)
        self.assertEqual(result, (False, "stuck"))


if __name__ == "__main__":
    unittest.main()
