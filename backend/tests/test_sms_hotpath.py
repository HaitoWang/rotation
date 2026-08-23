"""接码热路径的并发/选号回归测试。

线上 600 并发时 py-spy 抓到 1235 个线程里有 542 个同时卡在目录排序这条
路径上，每次换号平均排队 484s；真正的取号只要 1s、等码只要 22s。这些
测试锁住修复后的行为，避免热路径上再被加回全局串行或全量重算。
"""
import json
import threading
import time
import unittest
from unittest import mock

from app import sms_provider


class _CountingProvider:
    """记录目录 API 被真正调用了几次的假供应商。"""

    platform_key = "smsbower"
    max_price = 0.1
    supplier_strategy = "success_first"
    default_service = "dr"
    default_country = "52"

    def __init__(self, delay=0.05):
        self.calls = 0
        self.delay = delay
        self._lock = threading.Lock()
        self._ranked_providers_by_country = {}

    def _request(self, params):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        raise RuntimeError("no network in tests")


def _reset_caches():
    sms_provider._SMS_CATALOG_CACHE.clear()
    sms_provider._SMS_RANKED_CACHE.clear()
    sms_provider._SMS_QUALITY_CACHE.clear()
    sms_provider._SMS_CATALOG_FETCH_LOCKS.clear()
    sms_provider._SMS_CATALOG_DISK_CACHE.clear()


class CatalogSingleFlightTests(unittest.TestCase):
    def setUp(self):
        _reset_caches()

    def test_catalog_refresh_is_single_flight_not_a_global_queue(self):
        """并发刷新只应发一次请求，且后来者不必排在前者后面串行。"""
        provider = sms_provider.SmsBowerProvider(api_key="k", max_price=0.1)
        calls = []
        gate = threading.Event()

        def _slow_refresh(service_code, cache_key):
            calls.append(service_code)
            gate.wait(2.0)
            rows = [{"country": "39", "provider_id": 0, "price": 0.05, "count": 100}]
            with sms_provider._SMS_CATALOG_LOCK:
                sms_provider._SMS_CATALOG_CACHE[cache_key] = {
                    "saved_at": time.time(), "rows": rows
                }
            return provider._apply_provider_catalog(rows)

        with mock.patch.object(provider, "_refresh_top_countries", _slow_refresh):
            threads = [
                threading.Thread(target=lambda: provider.get_top_countries("dr"))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(5.0)
                self.assertFalse(t.is_alive(), "worker 卡在目录锁上没退出")

        self.assertEqual(len(calls), 1, "并发刷新应当只打一次上游 API")

    def test_cache_hit_does_not_touch_the_fetch_lock(self):
        """缓存新鲜时必须完全无锁返回，否则 600 并发会重新排队。"""
        provider = sms_provider.SmsBowerProvider(api_key="k", max_price=0.1)
        cache_key = f"{provider.platform_key}:dr:{provider.max_price}:{provider.supplier_strategy}"
        rows = [{"country": "39", "provider_id": 0, "price": 0.05, "count": 100}]
        with sms_provider._SMS_CATALOG_LOCK:
            sms_provider._SMS_CATALOG_CACHE[cache_key] = {
                "saved_at": time.time(), "rows": rows
            }
        held = sms_provider._catalog_fetch_lock(cache_key)
        held.acquire()
        try:
            done = threading.Event()

            def _read():
                provider.get_top_countries("dr")
                done.set()

            t = threading.Thread(target=_read)
            t.start()
            self.assertTrue(done.wait(2.0), "缓存命中仍被 fetch 锁挡住")
            t.join(2.0)
        finally:
            held.release()

    def test_refresh_failure_does_not_leak_the_lock(self):
        provider = sms_provider.SmsBowerProvider(api_key="k", max_price=0.1)
        cache_key = f"{provider.platform_key}:dr:{provider.max_price}:{provider.supplier_strategy}"
        with mock.patch.object(
            provider, "_refresh_top_countries", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                provider.get_top_countries("dr")
        lock = sms_provider._catalog_fetch_lock(cache_key)
        self.assertTrue(lock.acquire(timeout=1.0), "刷新抛异常后锁没释放")
        lock.release()

    def test_stale_cache_serves_latecomers_instead_of_blocking(self):
        """缓存过期且有人在刷新时，后来者拿旧目录继续跑，不排队等刷新。

        线上实测：单飞改成阻塞等待后，636 个线程里仍有 185 个堵在 fetch 锁上，
        因为上游目录接口偶尔要几十秒。目录是小时级才变的数据，宁可用旧的。
        """
        provider = sms_provider.SmsBowerProvider(api_key="k", max_price=0.1)
        cache_key = f"{provider.platform_key}:dr:{provider.max_price}:{provider.supplier_strategy}"
        rows = [{"country": "39", "provider_id": 0, "price": 0.05, "count": 100}]
        with sms_provider._SMS_CATALOG_LOCK:
            sms_provider._SMS_CATALOG_CACHE[cache_key] = {
                # 故意写成过期的
                "saved_at": time.time() - sms_provider._SMS_CATALOG_TTL - 5,
                "rows": rows,
            }
        held = sms_provider._catalog_fetch_lock(cache_key)
        held.acquire()  # 模拟另一个 worker 正在慢慢刷新
        try:
            result = {}
            done = threading.Event()

            def _read():
                result["rows"] = provider.get_top_countries("dr")
                done.set()

            t = threading.Thread(target=_read)
            t.start()
            self.assertTrue(done.wait(2.0), "后来者被刷新锁堵住了，没有走陈旧兜底")
            t.join(2.0)
            self.assertTrue(result.get("rows"), "陈旧兜底应当返回可用的国家列表")
        finally:
            held.release()

    def test_disk_catalog_is_parsed_once_per_ttl(self):
        """磁盘目录回退路径也要缓存，否则几百 worker 各解析一遍 1700 行 JSON。"""
        provider = sms_provider.SmsBowerProvider(api_key="k", max_price=0.1)
        sms_provider._SMS_CATALOG_DISK_CACHE.clear()
        payload = json.dumps({
            "service": "dr",
            "saved_at": time.time(),
            "rows": [{"country": "39", "provider_id": 0, "price": 0.05, "count": 100}],
        })
        reads = []

        class _Spy:
            def read_text(self, encoding="utf-8"):
                reads.append(1)
                return payload

        with mock.patch.object(sms_provider, "_sms_catalog_file", return_value=_Spy()):
            first = provider._load_provider_catalog("dr")
            second = provider._load_provider_catalog("dr")
        self.assertTrue(first and second)
        self.assertEqual(len(reads), 1, "TTL 内磁盘目录应当只解析一次")
        first[0]["country"] = "tampered"
        with mock.patch.object(sms_provider, "_sms_catalog_file", return_value=_Spy()):
            third = provider._load_provider_catalog("dr")
        self.assertEqual(third[0]["country"], "39", "缓存返回的行必须是副本")

    def test_cold_start_wait_times_out_instead_of_pinning_the_worker(self):
        """连磁盘目录都没有的平台，冷启动等待必须超时放弃。

        线上 herosms 没有磁盘目录、上游又拿不到数据，224/635 个线程无限期
        钉在 fetch_lock.acquire() 上，把 split 模式另一半算力也废掉了。
        超时抛错后 _acquire_split 会切到下一个平台，worker 得以继续。
        """
        provider = sms_provider.SmsBowerProvider(api_key="k", max_price=0.1)
        cache_key = f"{provider.platform_key}:dr:{provider.max_price}:{provider.supplier_strategy}"
        held = sms_provider._catalog_fetch_lock(cache_key)
        held.acquire()  # 模拟持锁者卡在慢上游
        try:
            with mock.patch.object(provider, "_load_provider_catalog", return_value=[]), \
                 mock.patch.object(sms_provider, "_SMS_CATALOG_COLD_WAIT", 0.2):
                started = time.time()
                with self.assertRaises(RuntimeError):
                    provider.get_top_countries("dr")
                waited = time.time() - started
            self.assertLess(waited, 2.0, f"冷启动等了 {waited:.1f}s，没有按超时放弃")
        finally:
            held.release()


class CatalogFallbackPersistsTests(unittest.TestCase):
    """不支持 getPricesV3 的平台，兜底目录也必须落缓存。

    线上 herosms 的 getPricesV3 直接 404，而兜底分支以前只 return、不写缓存，
    于是内存缓存和磁盘目录永远是空的：每个 worker 都走冷启动、撞上 8s 超时被
    _acquire_split 跳过，1:1 分流退化成 1:0，只有 smsbower 在接码。
    """

    def setUp(self):
        _reset_caches()

    def _provider(self):
        return sms_provider.SmsBowerProvider(
            api_key="k", platform_key="herosms", max_price=0.1
        )

    def test_getprices_fallback_populates_caches(self):
        provider = self._provider()
        cache_key = f"{provider.platform_key}:dr:{provider.max_price}:{provider.supplier_strategy}"
        saved = {}

        def _request(params):
            action = params.get("action")
            if action == "getPricesV3":
                raise RuntimeError("404 Not Found")
            if action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
                raise RuntimeError("429 Too Many Requests")
            raise AssertionError(f"意外的 action: {action}")

        prices = {"86": {"dr": {"cost": 0.09, "count": 3460}},
                  "62": {"dr": {"cost": 0.35, "count": 5781}}}

        class _Sink:
            def write_text(self, data, encoding="utf-8"):
                saved.update(json.loads(data))

        with mock.patch.object(provider, "_request", _request), \
             mock.patch.object(provider, "get_prices", return_value=prices), \
             mock.patch.object(sms_provider, "_sms_catalog_file", return_value=_Sink()):
            rows = provider.get_top_countries("dr")

        self.assertTrue(rows, "兜底目录应当产出候选国家")
        # max_price=0.1 会滤掉 0.35 的那个国家
        self.assertEqual([r["country"] for r in rows], ["86"])
        with sms_provider._SMS_CATALOG_LOCK:
            cached = sms_provider._SMS_CATALOG_CACHE.get(cache_key)
        self.assertIsNotNone(cached, "兜底目录没有写进内存缓存，下次仍会冷启动")
        self.assertTrue(cached["rows"], "内存缓存里的目录行不能是空的")
        self.assertEqual(saved.get("service"), "dr", "兜底目录没有落盘")
        self.assertEqual(len(saved.get("rows") or []), 2, "落盘应当保留未过滤的原始行")

    def test_second_call_hits_cache_without_new_upstream_requests(self):
        """落缓存之后，第二次取号不该再打上游，也不该再走冷启动分支。"""
        provider = self._provider()
        calls = []

        def _request(params):
            calls.append(params.get("action"))
            raise RuntimeError("no v3")

        prices = {"86": {"dr": {"cost": 0.09, "count": 3460}}}
        with mock.patch.object(provider, "_request", _request), \
             mock.patch.object(provider, "get_prices", return_value=prices), \
             mock.patch.object(sms_provider, "_sms_catalog_file",
                               return_value=mock.Mock(write_text=lambda *a, **k: None)):
            provider.get_top_countries("dr")
            first = len(calls)
            provider.get_top_countries("dr")
        self.assertEqual(len(calls), first, "缓存命中后仍在打上游目录接口")


class QualityMapCacheTests(unittest.TestCase):
    def setUp(self):
        _reset_caches()

    def test_quality_map_is_reused_until_stats_change(self):
        sms_provider._country_quality_map("smsbower")
        with mock.patch.object(sms_provider, "_load_stats", side_effect=AssertionError(
            "统计没变化时不应重新全表聚合"
        )):
            sms_provider._country_quality_map("smsbower")

    def test_recording_a_result_invalidates_the_quality_map(self):
        sms_provider._country_quality_map("smsbower")
        before = sms_provider._SMS_STATS_VERSION
        sms_provider._record_stats("smsbower", "39", 0, success=True)
        self.assertGreater(sms_provider._SMS_STATS_VERSION, before)
        quality = sms_provider._country_quality_map("smsbower")
        self.assertIn("39", quality)


class PlatformBanCheckTests(unittest.TestCase):
    """_is_platform_banned 在每次取号前都跑，不能碰磁盘。"""

    def setUp(self):
        sms_provider._SMS_BANS.clear()
        sms_provider._SMS_BANS_LOADED_AT = 0.0

    def tearDown(self):
        sms_provider._SMS_BANS.clear()
        sms_provider._SMS_BANS_LOADED_AT = 0.0

    def test_no_ban_does_not_reread_the_file_every_call(self):
        """没有封禁是常态；旧代码的 `if _SMS_BANS: return` 在这种情况下永不短路。"""
        reads = []

        class _Spy:
            def read_text(self, encoding="utf-8"):
                reads.append(1)
                return "{}"

            def write_text(self, data, encoding="utf-8"):
                raise AssertionError("没有封禁时不该写盘")

        with mock.patch.object(sms_provider, "_sms_ban_file", return_value=_Spy()):
            for _ in range(50):
                self.assertFalse(sms_provider._is_platform_banned("smsbower"))
        self.assertLessEqual(len(reads), 1, f"读了 {len(reads)} 次磁盘，节流没生效")

    def test_an_active_ban_is_still_reported(self):
        sms_provider._mark_platform_banned("herosms", reason="test")
        self.assertTrue(sms_provider._is_platform_banned("herosms"))
        self.assertFalse(sms_provider._is_platform_banned("smsbower"))

    def test_expired_ban_is_cleared(self):
        sms_provider._SMS_BANS["herosms"] = time.time() - 1
        sms_provider._SMS_BANS_LOADED_AT = time.time()
        self.assertFalse(sms_provider._is_platform_banned("herosms"))
        self.assertNotIn("herosms", sms_provider._SMS_BANS)


class CountryRotationTests(unittest.TestCase):
    def setUp(self):
        _reset_caches()

    def test_rotation_does_not_pair_a_great_country_with_a_barely_passing_one(self):
        """90% 和 16% 都在 tier 0，但不能被当作等价候选轮着用。"""
        quality = {"39": (0.90, 400), "37": (0.16, 700)}
        with mock.patch.object(sms_provider, "_country_quality_map", return_value=quality):
            seen = set()
            for _ in range(6):
                order = sms_provider._rotate_country_ids("smsbower", ["39", "37"])
                seen.add(order[0])
        self.assertEqual(seen, {"39"}, "低成功率国家不应被轮到第一顺位")

    def test_rotation_still_spreads_across_genuinely_close_countries(self):
        """成功率接近时仍要轮转，否则并发会全压在同一个国家上。"""
        quality = {"39": (0.90, 400), "68": (0.86, 300)}
        with mock.patch.object(sms_provider, "_country_quality_map", return_value=quality):
            seen = set()
            for _ in range(6):
                order = sms_provider._rotate_country_ids("smsbower", ["39", "68"])
                seen.add(order[0])
        self.assertEqual(seen, {"39", "68"}, "接近的国家之间应当轮转分散压力")


if __name__ == "__main__":
    unittest.main()
