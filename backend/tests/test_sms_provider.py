import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app.integrations.sms.provider as sms_provider


class SmsProviderFactoryTests(unittest.TestCase):
    def test_legacy_single_platform_config_still_works(self):
        providers = sms_provider.get_enabled_sms_providers({
            "sms_provider": "smsbower",
            "sms_api_key": "legacy-key",
        })
        self.assertEqual([p.platform_key for p in providers], ["smsbower"])
        self.assertEqual(providers[0].api_key, "legacy-key")

    def test_dual_platform_config_builds_both_clients(self):
        providers = sms_provider.get_enabled_sms_providers({
            "sms_mode": "race",
            "sms_smsbower_enabled": True,
            "sms_smsbower_api_key": "bower-key",
            "sms_herosms_enabled": True,
            "sms_herosms_api_key": "hero-key",
        })
        self.assertEqual([p.platform_key for p in providers], ["smsbower", "herosms"])
        self.assertEqual(providers[1].base_url, "https://hero-sms.com/stubs/handler_api.php")

    def test_provider_price_is_always_capped_at_fifteen_cents(self):
        default_provider = sms_provider.create_sms_provider("smsbower", {
            "sms_provider": "smsbower",
            "sms_api_key": "key",
            "sms_max_price": "",
        })
        expensive_provider = sms_provider.create_sms_provider("smsbower", {
            "sms_provider": "smsbower",
            "sms_api_key": "key",
            "sms_max_price": "1.108",
        })
        cheaper_provider = sms_provider.create_sms_provider("smsbower", {
            "sms_provider": "smsbower",
            "sms_api_key": "key",
            "sms_max_price": "0.08",
        })

        self.assertEqual(default_provider.max_price, 0.15)
        self.assertEqual(expensive_provider.max_price, 0.15)
        self.assertEqual(cheaper_provider.max_price, 0.08)

    def test_get_number_request_sends_hard_price_cap(self):
        provider = sms_provider.create_sms_provider("smsbower", {
            "sms_provider": "smsbower",
            "sms_api_key": "key",
        })
        response = mock.Mock(status_code=200, text="ACCESS_NUMBER:activation:66123456789")

        with mock.patch.object(provider, "_request", return_value=response) as request:
            provider._request_number_single_action("getNumber", "dr", "52")

        self.assertEqual(request.call_args.args[0]["maxPrice"], 0.15)


class SmsProviderRankingTests(unittest.TestCase):
    def setUp(self):
        sms_provider._SMS_CACHE.clear()
        sms_provider._SMS_STATS = {}
        sms_provider._SMS_BATCH_STATS.clear()
        sms_provider._SMS_INFLIGHT.clear()
        sms_provider._SMS_COOLDOWNS.clear()
        sms_provider._SMS_BLACKLIST.clear()
        sms_provider._SMS_BLACKLIST_LOADED = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)
        self.cache_patch = mock.patch.object(sms_provider, "_project_cache_dir", return_value=self.cache_dir)
        self.cache_patch.start()
        self.cleanup_patches = [
            mock.patch.object(sms_provider, "_track_sms_activation"),
            mock.patch.object(sms_provider, "_queue_sms_activation_cancel"),
            mock.patch.object(sms_provider, "_complete_sms_activation_cleanup"),
        ]
        for patcher in self.cleanup_patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.cleanup_patches):
            patcher.stop()
        self.cache_patch.stop()
        self.temp_dir.cleanup()

    def test_provider_prices_use_physical_count_and_score(self):
        provider = sms_provider.SmsBowerProvider("key", platform_key="herosms")
        rows = provider._parse_provider_prices({
            "52": {
                "dr": {
                    "101": {"price": 0.04, "count": 0, "physicalCount": 8},
                    "202": {"price": 0.03, "count": 2},
                }
            }
        }, "dr")
        self.assertEqual({row["provider_id"] for row in rows}, {101, 202})
        by_id = {row["provider_id"]: row for row in rows}
        self.assertEqual(by_id[101]["count"], 8)
        self.assertLess(by_id[101]["score"], by_id[202]["score"])

    def test_phone_cache_is_isolated_by_platform(self):
        bower = sms_provider.SmsBowerProvider("key-a", platform_key="smsbower")
        hero = sms_provider.SmsBowerProvider("key-b", platform_key="herosms")
        bower._save_cache({"activation_id": "a", "used_codes": set()})
        hero._save_cache({"activation_id": "b", "used_codes": set()})

        self.assertEqual(sms_provider._SMS_CACHE["smsbower"]["activation_id"], "a")
        self.assertEqual(sms_provider._SMS_CACHE["herosms"]["activation_id"], "b")
        self.assertTrue((self.cache_dir / ".sms_phone_cache_smsbower.json").exists())
        self.assertTrue((self.cache_dir / ".sms_phone_cache_herosms.json").exists())

    def test_batch_success_immediately_promotes_provider(self):
        cold = sms_provider._candidate_score("smsbower", "52", 101, 0.04, 20)
        sms_provider._record_stats("smsbower", "52", 202, success=True)
        hot = sms_provider._candidate_score("smsbower", "52", 202, 0.04, 20)
        self.assertLess(hot, cold)

    def test_success_first_strategy_ignores_price(self):
        cheap = sms_provider._candidate_score(
            "smsbower", "52", 101, 0.01, 20, "success_first"
        )
        expensive = sms_provider._candidate_score(
            "smsbower", "52", 202, 0.90, 20, "success_first"
        )
        self.assertAlmostEqual(cheap, expensive)

        balanced_cheap = sms_provider._candidate_score(
            "smsbower", "52", 101, 0.01, 20, "balanced"
        )
        balanced_expensive = sms_provider._candidate_score(
            "smsbower", "52", 202, 0.90, 20, "balanced"
        )
        self.assertLess(balanced_cheap, balanced_expensive)

    def test_final_rt_result_has_more_weight_than_sms_only_result(self):
        for _ in range(3):
            sms_provider._record_stats("smsbower", "52", 101, success=True)
            sms_provider._record_stats("smsbower", "52", 101, success=False, stage="rt")
            sms_provider._record_stats("smsbower", "52", 202, success=True)
            sms_provider._record_stats("smsbower", "52", 202, success=True, stage="rt")

        failed_rt = sms_provider._candidate_score(
            "smsbower", "52", 101, 0.04, 20, "success_first"
        )
        successful_rt = sms_provider._candidate_score(
            "smsbower", "52", 202, 0.04, 20, "success_first"
        )
        self.assertLess(successful_rt, failed_rt)

    def test_inflight_pressure_distributes_concurrent_requests(self):
        baseline = sms_provider._candidate_score(
            "smsbower", "52", 101, 0.04, 20, "success_first"
        )
        keys = [sms_provider._reserve_candidate("smsbower", "52", 101) for _ in range(10)]
        loaded = sms_provider._candidate_score(
            "smsbower", "52", 101, 0.04, 20, "success_first"
        )
        for key in keys:
            sms_provider._release_candidate(key)
        self.assertGreater(loaded, baseline)

    def test_cached_supplier_catalog_preserves_provider_ids(self):
        provider = sms_provider.SmsBowerProvider("key", platform_key="smsbower")
        rows = [{
            "country": "52", "provider_id": 303, "provider_name": "cached",
            "price": 0.08, "count": 12,
        }]
        provider._save_provider_catalog("dr", rows)
        loaded = provider._load_provider_catalog("dr")
        self.assertEqual(loaded[0]["provider_id"], 303)

    def test_adaptive_timeout_matches_gpt_plus_strategy(self):
        self.assertEqual(sms_provider._adaptive_timeout("smsbower", "52", 101), 35)
        sms_provider._record_stats("smsbower", "52", 101, success=True)
        self.assertEqual(sms_provider._adaptive_timeout("smsbower", "52", 101), 55)
        sms_provider._SMS_BATCH_STATS[sms_provider._stats_key("smsbower", "52", 101)] = {
            "success": 0,
            "failure": 3,
        }
        self.assertEqual(sms_provider._adaptive_timeout("smsbower", "52", 101), 20)

    def test_blacklisted_rental_is_cancelled_and_replaced(self):
        provider = sms_provider.SmsBowerProvider("key", platform_key="smsbower")
        provider._ranked_providers_by_country = {
            "52": [{"provider_id": 101, "price": 0.04, "count": 20}]
        }
        sms_provider._add_phone_blacklist("+66111111111")
        rentals = [
            {"activationId": "blocked", "phoneNumber": "66111111111"},
            {"activationId": "fresh", "phoneNumber": "66222222222"},
        ]
        with mock.patch.object(provider, "_request_number_single_action", side_effect=rentals) as rent, \
                mock.patch.object(provider, "cancel", return_value=True) as cancel:
            activation = provider.get_number(service="dr", country_candidates=["52"])

        self.assertEqual(activation.activation_id, "fresh")
        self.assertEqual(rent.call_count, 2)
        cancel.assert_called_once_with("blocked", record_failure=False)

    def test_no_numbers_uses_short_inventory_cooldown(self):
        provider = sms_provider.SmsBowerProvider("key", platform_key="herosms")
        provider._ranked_providers_by_country = {
            "52": [{"provider_id": 202, "price": 0.04, "count": 20}]
        }
        with mock.patch.object(
            provider, "_request_number_single_action", side_effect=RuntimeError("NO_NUMBERS")
        ):
            with self.assertRaises(RuntimeError):
                provider.get_number(service="dr", country_candidates=["52"])

        until = sms_provider._SMS_COOLDOWNS[sms_provider._stats_key("herosms", "52", 202)]
        remaining = until - time.time()
        self.assertGreater(remaining, 25)
        self.assertLess(remaining, 60)

    def test_hero_uses_all_live_countries_and_orders_by_price(self):
        provider = sms_provider.SmsBowerProvider(
            "key", platform_key="herosms", supplier_strategy="success_first"
        )
        rows = [
            {"country": "18", "price": 0.35, "count": 1400, "score": 1.0},
            {"country": "32", "price": 0.20, "count": 0, "score": 2.0},
            {"country": "4", "price": 0.02, "count": 5000, "score": 0.1},
            {"country": "82", "price": 0.25, "count": 500, "score": 0.2},
        ]
        controller = sms_provider.PhoneCallbackController(
            "herosms",
            {
                "sms_supplier_strategy": "success_first",
                "sms_auto_min_stock": "20",
                "sms_auto_max_price": "0.06",
                "sms_strict_whitelist": "0",
            },
            service="dr",
            auto_select_country=True,
        )
        with mock.patch.object(provider, "get_top_countries", return_value=rows), \
                mock.patch.object(
                    sms_provider,
                    "_country_quality_map",
                    return_value={
                        "82": (0.90, 820),
                        "18": (0.95, 100),
                        "32": (0.80, 100),
                        "4": (0.01, 5000),
                    },
                ):
            candidates = controller._country_candidates(provider)

        self.assertEqual(candidates, ["4", "32", "82", "18"])

    def test_hero_preserves_price_order_when_renting(self):
        provider = sms_provider.SmsBowerProvider(
            "key", platform_key="herosms", supplier_strategy="success_first"
        )
        provider._ranked_providers_by_country = {
            "4": [{"provider_id": 101, "price": 0.02, "count": 20}],
            "18": [{"provider_id": 202, "price": 0.35, "count": 20}],
        }
        calls = []

        def _rent(_action, _service, country, provider_id=0):
            calls.append((country, provider_id))
            if country == "4":
                raise RuntimeError("NO_NUMBERS")
            return {"activationId": "paid", "phoneNumber": "33123456789"}

        with mock.patch.object(provider, "_request_number_single_action", side_effect=_rent), \
                mock.patch.object(
                    sms_provider,
                    "_country_quality_map",
                    return_value={"4": (0.01, 5000), "18": (0.99, 5000)},
                ):
            activation = provider.get_number(
                service="dr", country_candidates=["4", "18"]
            )

        self.assertEqual(activation.country, "18")
        self.assertEqual([country for country, _ in calls], ["4", "18"])

    def test_hero_uses_cheapest_provider_inside_country(self):
        provider = sms_provider.SmsBowerProvider(
            "key", platform_key="herosms", supplier_strategy="success_first"
        )
        provider._ranked_providers_by_country = {
            "52": [
                {"provider_id": 101, "price": 0.20, "count": 50},
                {"provider_id": 202, "price": 0.03, "count": 50},
            ]
        }
        calls = []

        def _rent(_action, _service, country, provider_id=0):
            calls.append((country, provider_id))
            return {"activationId": "cheap", "phoneNumber": "66123456789"}

        with mock.patch.object(provider, "_request_number_single_action", side_effect=_rent):
            provider.get_number(service="dr", country_candidates=["52"])

        self.assertEqual(calls, [("52", 202)])

    def test_cancel_is_queued_before_remote_call_and_removed_on_success(self):
        provider = sms_provider.SmsBowerProvider("key", platform_key="herosms")
        provider.current_activation = sms_provider.SmsActivation(
            "activation-1",
            "+66123456789",
            "52",
            {"acquired_at": time.time() - 130},
        )
        queued = mock.Mock()
        completed = mock.Mock()
        response = mock.Mock(status_code=204, text="")
        with mock.patch.object(sms_provider, "_queue_sms_activation_cancel", queued), \
                mock.patch.object(sms_provider, "_complete_sms_activation_cleanup", completed), \
                mock.patch.object(provider, "_request", return_value=response):
            self.assertTrue(provider.cancel("activation-1", record_failure=False))

        queued.assert_called_once()
        completed.assert_called_once_with("herosms", "activation-1")

    def test_hero_early_cancel_waits_for_background_window(self):
        provider = sms_provider.SmsBowerProvider("key", platform_key="herosms")
        provider.current_activation = sms_provider.SmsActivation(
            "activation-early",
            "+66123456789",
            "52",
            {"acquired_at": time.time()},
        )
        queued = mock.Mock()
        with mock.patch.object(sms_provider, "_queue_sms_activation_cancel", queued), \
                mock.patch.object(provider, "_request") as request:
            self.assertFalse(provider.cancel("activation-early", record_failure=False))

        queued.assert_called_once()
        request.assert_not_called()

    def test_non_reused_numbers_do_not_take_a_global_lock_or_cache(self):
        provider = sms_provider.SmsBowerProvider(
            "key", platform_key="smsbower", reuse_phone_to_max=False
        )
        provider._ranked_providers_by_country = {
            "52": [{"provider_id": 101, "price": 0.04, "count": 20}]
        }
        with mock.patch.object(provider, "_request_number_single_action", return_value={
            "activationId": "independent",
            "phoneNumber": "66333333333",
        }):
            provider.get_number(service="dr", country_candidates=["52"])

        controller = sms_provider.PhoneCallbackController("smsbower", {})
        controller._acquire_reuse_locks([provider])
        self.assertEqual(controller._acquired_reuse_locks, [])
        self.assertNotIn("smsbower", sms_provider._SMS_CACHE)


class _FakeRaceProvider:
    def __init__(self, platform_key, delay, phone, score=None):
        self.platform_key = platform_key
        self.delay = delay
        self.phone = phone
        self.score = score
        self.cancelled = []
        self.calls = 0  # 记录是否真被启动过（对冲窗口内主平台成功时应为 0）

    def get_number(self, **_kwargs):
        self.calls += 1
        time.sleep(self.delay)
        metadata = {"platform": self.platform_key}
        if self.score is not None:
            metadata["selection_score"] = self.score
        return sms_provider.SmsActivation(
            f"activation-{self.platform_key}", self.phone, "52", metadata
        )

    def cancel(self, activation_id, *, record_failure=True):
        self.cancelled.append((activation_id, record_failure))
        return True

    def report_success(self, _activation_id):
        return True

    def set_resend_callback(self, _callback):
        return None


class SmsRaceTests(unittest.TestCase):
    def test_hedge_window_keeps_second_platform_unused_when_first_is_fast(self):
        """对冲契约：主平台在 hedge 窗口内成功，第二平台根本不启动。

        这是「每个任务默认只烧一个号」的关键 —— 旧实现无条件并发拉两个号再
        取消输家，等于把号消耗翻倍。所以这里断言的是 herosms 完全没被调用
        （cancelled 为空是因为压根没取号，不是因为漏了取消）。
        """
        fast = _FakeRaceProvider("smsbower", 0.01, "+66111111111")
        slow = _FakeRaceProvider("herosms", 0.08, "+66222222222")
        controller = sms_provider.PhoneCallbackController(
            "smsbower", {"sms_mode": "race"}, service="dr", country="52"
        )
        with mock.patch.object(sms_provider, "get_enabled_sms_providers", return_value=[fast, slow]):
            self.assertEqual(controller.get_phone(), "+66111111111")
            time.sleep(0.12)
            controller.report_success()

        self.assertEqual(controller.provider_key, "smsbower")
        self.assertEqual(slow.cancelled, [])
        self.assertEqual(slow.calls, 0)

    def test_slow_first_platform_triggers_hedge_and_loser_is_cancelled(self):
        """主平台慢过 hedge 窗口 → 启动第二平台；晚到的输家必须被取消退款。"""
        slow = _FakeRaceProvider("smsbower", 0.25, "+66111111111")
        quick = _FakeRaceProvider("herosms", 0.01, "+66222222222")
        controller = sms_provider.PhoneCallbackController(
            "smsbower",
            {"sms_mode": "race", "sms_race_hedge_ms": 20},
            service="dr",
            country="52",
        )
        with mock.patch.object(
            sms_provider, "get_enabled_sms_providers", return_value=[slow, quick]
        ):
            self.assertEqual(controller.get_phone(), "+66222222222")
            time.sleep(0.35)  # 等主平台晚到，走 _cancel_late_activation

        self.assertEqual(controller.provider_key, "herosms")
        self.assertEqual(slow.cancelled, [("activation-smsbower", False)])

    def test_quality_can_beat_raw_acquisition_speed(self):
        """balanced 策略下才比质量：hedge=0 让两边同时起跑，决策窗口内择优。

        success_first（默认）会把决策窗口强制压成 0 —— 先到就用，不比价。
        想要「质量赢过速度」必须显式选 balanced，否则 sms_race_decision_ms
        根本不生效。
        """
        fast = _FakeRaceProvider("smsbower", 0.01, "+66111111111", score=5.0)
        quality = _FakeRaceProvider("herosms", 0.08, "+66222222222", score=0.2)
        controller = sms_provider.PhoneCallbackController(
            "smsbower",
            {
                "sms_mode": "race",
                "sms_supplier_strategy": "balanced",
                "sms_race_hedge_ms": 0,
                "sms_race_decision_ms": 400,
            },
            service="dr",
            country="52",
        )
        with mock.patch.object(
            sms_provider, "get_enabled_sms_providers", return_value=[fast, quality]
        ):
            self.assertEqual(controller.get_phone(), "+66222222222")

        self.assertEqual(controller.provider_key, "herosms")
        self.assertEqual(fast.cancelled, [("activation-smsbower", False)])


if __name__ == "__main__":
    unittest.main()
