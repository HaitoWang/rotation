import unittest

from app.webui.auto_loop import AutoLoopController, AutoLoopState


class AutoLoopResilienceTests(unittest.TestCase):
    def test_network_failures_never_auto_pause(self):
        controller = AutoLoopController()
        controller._state = AutoLoopState.RUNNING

        for _ in range(5):
            controller._record_finish(False, "network")

        self.assertEqual(controller._state, AutoLoopState.RUNNING)
        self.assertFalse(controller._pause_event.is_set())

    def test_retry_offset_rotates_proxy_pool(self):
        controller = AutoLoopController()
        controller._proxy_pool = ["proxy-a", "proxy-b", "proxy-c"]

        self.assertEqual(controller._proxy_for_worker(0, 0), "proxy-a")
        self.assertEqual(controller._proxy_for_worker(0, 1), "proxy-b")
        self.assertEqual(controller._proxy_for_worker(0, 3), "proxy-a")

    def test_concurrency_is_not_capped_at_twenty(self):
        controller = AutoLoopController()
        controller._manage_loop = lambda: None

        result = controller.start({"concurrency": 128})

        self.assertTrue(result["ok"])
        self.assertEqual(result["concurrency"], 128)


if __name__ == "__main__":
    unittest.main()
