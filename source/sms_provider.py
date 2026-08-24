"""SMS 接码 provider 抽象 + SmsBower 实现。

设计参考：asz798838958/GeniusFKoai 的 core/base_sms.py，但裁剪掉浏览器回调相关代码、
仅保留纯协议注册需要的两段流程：
    1) rent number    → provider.get_number(service=..., country=...)
    2) wait sms code  → provider.get_code(activation_id, timeout=...)
    3) 成功/失败       → provider.report_success / cancel / mark_code_failed

⚠️ 关键事实：OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证，**纯 SMS 路径目前只有
泰国（country_id=52）确认可用**。其它国家可能抽到 WhatsApp 号导致拿不到 SMS。
SmsBower 的 `auto_select_country=True` 会按价格 + 库存自动选号。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


@dataclass
class SmsActivation:
    """一次手机号租用的句柄。"""
    activation_id: str
    phone_number: str          # E.164 格式，带 + 前缀
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """接码 provider 抽象基类。"""

    auto_report_success_on_code = True  # True = 收到 code 即报成功；False = 等业务侧确认

    @abstractmethod
    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str, *, record_failure: bool = True) -> bool:
        ...

    def get_balance(self) -> float:
        """查询余额（货币随平台）。"""
        raise NotImplementedError

    def report_success(self, activation_id: str) -> bool:
        """业务侧验证通过后调用，平台可能据此结算/允许复用。"""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧收到 code 但 validate 失败 → 请求 resend。"""
        return None

    def mark_send_failed(
        self, activation_id: str, reason: str = "", *, record_failure: Optional[bool] = None
    ) -> None:
        """业务侧拒绝该手机号（add-phone/send 返错）→ 停止复用。"""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """业务侧已成功触发短信发送（add-phone/send 200）。"""
        return None

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """注册 resend 钩子（SmsBower 长等待时回调业务侧重新触发 OTP）。"""
        return None

    def set_should_stop(self, callback: Optional[Callable[[], bool]]) -> None:
        """设置轮询停止检查；不支持中断的 provider 保持兼容空实现。"""
        return None


# ---------------------------------------------------------------------------
# 国家 ID → 中文名映射（sms-activate.org 协议系，SmsBower 共用）
# ---------------------------------------------------------------------------

SMS_COUNTRY_NAMES_CN: dict[str, str] = {
    "0": "俄罗斯", "1": "乌克兰", "2": "哈萨克斯坦", "3": "中国", "4": "菲律宾",
    "5": "缅甸", "6": "印度尼西亚", "7": "马来西亚", "8": "肯尼亚", "9": "坦桑尼亚",
    "10": "越南", "11": "吉尔吉斯斯坦", "12": "美国(虚拟)", "13": "以色列", "14": "香港",
    "15": "波兰", "16": "英国", "17": "马达加斯加", "18": "刚果(布)", "19": "尼日利亚",
    "20": "澳门", "21": "埃及", "22": "印度", "23": "爱尔兰", "24": "柬埔寨",
    "25": "老挝", "26": "海地", "27": "科特迪瓦", "28": "冈比亚", "29": "塞尔维亚",
    "30": "也门", "31": "南非", "32": "罗马尼亚", "33": "哥伦比亚", "34": "爱沙尼亚",
    "35": "阿塞拜疆", "36": "加拿大", "37": "摩洛哥", "38": "加纳", "39": "阿根廷",
    "40": "乌兹别克斯坦", "41": "喀麦隆", "42": "乍得", "43": "德国", "44": "立陶宛",
    "45": "克罗地亚", "46": "瑞典", "47": "伊拉克", "48": "荷兰", "49": "拉脱维亚",
    "50": "奥地利", "51": "白俄罗斯", "52": "泰国", "53": "沙特阿拉伯", "54": "墨西哥",
    "55": "台湾", "56": "西班牙", "57": "伊朗", "58": "阿尔及利亚", "59": "斯洛文尼亚",
    "60": "孟加拉国", "61": "塞内加尔", "62": "土耳其", "63": "捷克", "64": "斯里兰卡",
    "65": "秘鲁", "66": "巴基斯坦", "67": "新西兰", "68": "几内亚", "69": "马里",
    "70": "委内瑞拉", "71": "埃塞俄比亚", "72": "蒙古", "73": "巴西", "74": "阿富汗",
    "75": "乌干达", "76": "安哥拉", "77": "塞浦路斯", "78": "法国", "79": "巴布亚新几内亚",
    "80": "莫桑比克", "81": "尼泊尔", "82": "比利时", "83": "保加利亚", "84": "匈牙利",
    "85": "摩尔多瓦", "86": "意大利", "87": "巴拉圭", "88": "洪都拉斯", "89": "突尼斯",
    "90": "尼加拉瓜", "91": "东帝汶", "92": "玻利维亚", "93": "哥斯达黎加", "94": "危地马拉",
    "95": "阿联酋", "96": "津巴布韦", "97": "波多黎各", "98": "苏丹", "99": "多哥",
    "100": "科威特", "101": "萨尔瓦多", "102": "利比亚", "103": "牙买加", "104": "特立尼达和多巴哥",
    "105": "厄瓜多尔", "106": "斯威士兰", "107": "阿曼", "108": "波黑", "109": "多米尼加",
    "110": "叙利亚", "111": "卡塔尔", "112": "巴拿马", "113": "古巴", "114": "毛里塔尼亚",
    "115": "塞拉利昂", "116": "约旦", "117": "葡萄牙", "118": "巴巴多斯", "119": "布隆迪",
    "120": "贝宁", "121": "文莱", "122": "巴哈马", "123": "博茨瓦纳", "124": "伯利兹",
    "125": "中非", "126": "多米尼克", "127": "格林纳达", "128": "格鲁吉亚", "129": "希腊",
    "130": "几内亚比绍", "131": "圭亚那", "132": "冰岛", "133": "科摩罗", "134": "利比里亚",
    "135": "莱索托", "136": "马拉维", "137": "纳米比亚", "138": "尼日尔", "139": "卢旺达",
    "140": "斯洛伐克", "141": "苏里南", "142": "塔吉克斯坦", "143": "摩纳哥", "144": "巴林",
    "145": "留尼汪岛", "146": "赞比亚", "147": "亚美尼亚", "148": "索马里", "149": "刚果(金)",
    "150": "智利", "151": "布基纳法索", "152": "黎巴嫩", "153": "加蓬", "154": "阿尔巴尼亚",
    "155": "乌拉圭", "156": "毛里求斯", "157": "不丹", "158": "马尔代夫", "159": "瓜德罗普岛",
    "160": "土库曼斯坦", "161": "法属圭亚那", "162": "芬兰", "163": "圣卢西亚", "164": "卢森堡",
    "165": "圣文森特", "166": "赤道几内亚", "167": "吉布提", "168": "安提瓜和巴布达", "169": "开曼群岛",
    "170": "黑山", "171": "丹麦", "172": "瑞士", "173": "挪威", "174": "澳大利亚",
    "175": "厄立特里亚", "176": "南苏丹", "177": "圣多美", "178": "阿鲁巴岛", "179": "蒙特塞拉特",
    "180": "安圭拉岛", "181": "北马其顿", "182": "塞舌尔", "183": "新喀里多尼亚", "184": "佛得角",
    "185": "美国(实体)", "186": "巴勒斯坦", "187": "美国", "188": "中国", "189": "韩国",
    "190": "科特迪瓦", "191": "日本",
}


def country_label(country_id) -> str:
    """返回 '52 泰国' 这样的展示标签。"""
    cid = str(country_id or "").strip()
    name = SMS_COUNTRY_NAMES_CN.get(cid, "")
    return f"{cid} {name}".strip()


# ---------------------------------------------------------------------------
# SmsBower / SMSBower —— 共享 API 协议
# ---------------------------------------------------------------------------

SMS_DEFAULT_SERVICE = "dr"
SMS_DEFAULT_COUNTRY = "52"  # Thailand —— OpenAI 走 SMS 的稳定国家
SMS_DEFAULT_SUPPLIER_STRATEGY = "success_first"
SMS_PHONE_LIFETIME = 20 * 60  # 号码租用窗口（秒）
HERO_CANCEL_MIN_AGE_SECONDS = 125  # HeroSMS 购买约 2 分钟后才允许主动取消
_SMS_CACHE_LOCK = threading.Lock()
_SMS_CACHE: dict[str, dict] = {}  # 按平台隔离，避免双平台互相覆盖号码租约
_SMS_PLATFORM_LOCKS_LOCK = threading.Lock()
_SMS_PLATFORM_LOCKS: dict[str, threading.RLock] = {}
_SMS_STATS_LOCK = threading.Lock()
_SMS_STATS: Optional[dict] = None
_SMS_BATCH_STATS: dict[str, dict[str, int]] = {}
_SMS_STATS_BUCKETS: Optional[dict[str, dict[str, dict]]] = None
_SMS_STATS_WINDOW: Optional[tuple[int, int]] = None
_SMS_STATS_SCHEMA_VERSION = 2
_SMS_STATS_RETENTION_SECONDS = 24 * 60 * 60
_SMS_STATS_RECENT_SECONDS = 60 * 60
_SMS_STATS_BUCKET_SECONDS = 15 * 60
_SMS_INFLIGHT_LOCK = threading.Lock()
_SMS_INFLIGHT: dict[str, int] = {}
_SMS_COOLDOWN_LOCK = threading.Lock()
_SMS_COOLDOWNS: dict[str, float] = {}
_SMS_BAN_LOCK = threading.Lock()
_SMS_BANS: dict[str, float] = {}
# 封禁表的读盘节流：_is_platform_banned 在每次取号前都会调用。
_SMS_BANS_LOADED_AT = 0.0
_SMS_BANS_RELOAD_TTL = 30.0
_SMS_BAN_SECONDS = 15 * 60
# Provider inventory catalogs are eventually consistent. A country reported
# as empty can become rentable seconds later (and the reverse is common), so
# a single NO_NUMBERS response must not park every worker for five minutes.
_SMS_INVENTORY_COOLDOWN_SECONDS = 30
# ``split`` 模式的进程内调度状态。每个启用平台集合独立计数，避免
# 主平台配置或另一组账号的调用把当前双平台分流比例带偏。
_SMS_SPLIT_LOCK = threading.Lock()
_SMS_SPLIT_STATE: dict[str, dict] = {}
_SMS_CATALOG_LOCK = threading.RLock()
_SMS_CATALOG_CACHE: dict[str, dict] = {}
_SMS_CATALOG_TTL = 30.0
# 磁盘目录的解析结果缓存（内存缓存过期时的回退路径），复用 _SMS_CATALOG_LOCK。
_SMS_CATALOG_DISK_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SMS_CATALOG_DISK_TTL = 60.0
# 冷启动（连磁盘目录都没有）时最多等多久刷新。超时就放弃这个平台，
# 让 worker 去另一个平台取号，而不是整条 split 链路一起卡死。
_SMS_CATALOG_COLD_WAIT = 8.0
# 目录刷新按 cache_key 单飞：一把全局互斥锁会让所有并发 worker 排成一列，
# 实测 600 并发时每次换号要排 8 分钟。改成每个 key 一把锁 + 双重检查，
# 只有真正需要刷新的那一个 worker 发请求，其余直接读缓存。
_SMS_CATALOG_FETCH_LOCKS: dict[str, threading.Lock] = {}
_SMS_CATALOG_FETCH_LOCKS_LOCK = threading.Lock()
# 排好序的候选国家列表也按 key 缓存。它由目录行 + 统计快照算出，
# 每个 worker 各算一遍纯属重复劳动，而且全在 GIL 下串行。
_SMS_RANKED_CACHE: dict[str, dict] = {}
_SMS_RANKED_TTL = 15.0
_SMS_RANKED_LOCK = threading.Lock()
_SMS_COUNTRY_CURSOR_LOCK = threading.Lock()
_SMS_COUNTRY_CURSORS: dict[str, int] = {}
_SMS_BLACKLIST_LOCK = threading.Lock()
_SMS_BLACKLIST: set[str] = set()
_SMS_BLACKLIST_LOADED = False
# 统计快照每次写入都会 +1；国家质量表按这个版本号缓存，
# 没有新的成败结果就直接复用，不再全表重扫 9 万条记录。
_SMS_STATS_VERSION = 0
_SMS_QUALITY_CACHE: dict[str, tuple[int, dict[str, tuple[float, int]]]] = {}
_SMS_QUALITY_LOCK = threading.Lock()

# OpenAI 走纯 SMS 的国家白名单（截至 2025-2026 实测；其它国家会抽到 WhatsApp 号）
OPENAI_SMS_COUNTRIES = {"52"}  # Thailand only

# 价格只能在成功率接近时作为次要因素。旧实现把原始价格直接乘进总分，
# 0.01 的低质量号码会轻易压过 0.10 的高质量号码。
_SMS_PRICE_REFERENCE = 0.06
_SMS_MIN_COUNTRY_SAMPLES = 20
_SMS_BAD_COUNTRY_RATE = 0.15
# tier 只把国家分成"好/未知/差"三档，档内按成功率轮转以分散并发压力。
# 但 0.15 的档线会把 90% 和 16% 的国家放进同一档当作等价候选轮着用，
# 线上因此频繁抽到低成功率号码白等一整个 60s 窗口。轮转只在成功率与
# 档内最优相差不超过这个幅度时才做。
_SMS_ROTATE_RATE_TOLERANCE = 0.12


def _catalog_fetch_lock(cache_key: str) -> threading.Lock:
    with _SMS_CATALOG_FETCH_LOCKS_LOCK:
        return _SMS_CATALOG_FETCH_LOCKS.setdefault(cache_key, threading.Lock())


def _stats_key(platform: str, country: str, provider_id: int = 0) -> str:
    return f"{platform}:{country}:{max(0, int(provider_id or 0))}"


def _platform_reuse_lock(platform: str) -> threading.RLock:
    key = str(platform or "smsbower").strip().lower()
    with _SMS_PLATFORM_LOCKS_LOCK:
        return _SMS_PLATFORM_LOCKS.setdefault(key, threading.RLock())


def _stats_bucket_floor(timestamp: float) -> int:
    return int(float(timestamp) // _SMS_STATS_BUCKET_SECONDS) * _SMS_STATS_BUCKET_SECONDS


def _stats_min_bucket(now: float, window_seconds: int) -> int:
    """返回完整落在窗口内的首个桶，边界桶宁可丢弃也不保留过期事件。"""
    cutoff = float(now) - max(1, int(window_seconds))
    floor = _stats_bucket_floor(cutoff)
    return floor if abs(cutoff - floor) < 1e-6 else floor + _SMS_STATS_BUCKET_SECONDS


def _aggregate_stats_buckets(
    buckets: dict[str, dict[str, dict]], min_bucket: int
) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for raw_bucket, candidate_rows in buckets.items():
        try:
            bucket = int(raw_bucket)
        except (TypeError, ValueError):
            continue
        if bucket < min_bucket or not isinstance(candidate_rows, dict):
            continue
        for key, source in candidate_rows.items():
            if not isinstance(source, dict):
                continue
            row = stats.setdefault(
                str(key), {"success": 0, "failure": 0, "rt_success": 0, "rt_failure": 0}
            )
            for field in ("success", "failure", "rt_success", "rt_failure"):
                row[field] += max(0, _safe_int(source.get(field), 0))
            row["updated_at"] = max(
                float(row.get("updated_at") or 0),
                _safe_float(source.get("updated_at"), float(bucket)),
            )
    return stats


def _write_stats_locked() -> None:
    if _SMS_STATS_BUCKETS is None:
        return
    payload = {
        "version": _SMS_STATS_SCHEMA_VERSION,
        "retention_seconds": _SMS_STATS_RETENTION_SECONDS,
        "bucket_seconds": _SMS_STATS_BUCKET_SECONDS,
        "buckets": _SMS_STATS_BUCKETS,
    }
    path = _sms_stats_file()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.debug("保存 SMS 24h 滚动统计失败", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _refresh_stats_window_locked(now: Optional[float] = None, *, force: bool = False) -> bool:
    global _SMS_STATS, _SMS_STATS_WINDOW
    if _SMS_STATS_BUCKETS is None:
        return False
    current = float(now if now is not None else time.time())
    retention_min = _stats_min_bucket(current, _SMS_STATS_RETENTION_SECONDS)
    recent_min = _stats_min_bucket(current, _SMS_STATS_RECENT_SECONDS)
    marker = (retention_min, recent_min)
    if not force and marker == _SMS_STATS_WINDOW:
        return False

    removed = False
    current_bucket = _stats_bucket_floor(current)
    for raw_bucket in list(_SMS_STATS_BUCKETS):
        try:
            bucket_value = int(raw_bucket)
            keep = retention_min <= bucket_value <= current_bucket
        except (TypeError, ValueError):
            keep = False
        if not keep:
            _SMS_STATS_BUCKETS.pop(raw_bucket, None)
            removed = True

    _SMS_STATS = _aggregate_stats_buckets(_SMS_STATS_BUCKETS, retention_min)
    recent = _aggregate_stats_buckets(_SMS_STATS_BUCKETS, recent_min)
    _SMS_BATCH_STATS.clear()
    _SMS_BATCH_STATS.update(recent)
    _SMS_STATS_WINDOW = marker
    if removed:
        _write_stats_locked()
    return removed


def _load_stats() -> dict:
    global _SMS_STATS, _SMS_STATS_BUCKETS, _SMS_STATS_WINDOW
    if _SMS_STATS is not None:
        _refresh_stats_window_locked()
        return _SMS_STATS

    is_v2 = False
    buckets: dict[str, dict[str, dict]] = {}
    try:
        raw = json.loads(_sms_stats_file().read_text(encoding="utf-8"))
        is_v2 = (
            isinstance(raw, dict)
            and int(raw.get("version") or 0) == _SMS_STATS_SCHEMA_VERSION
            and isinstance(raw.get("buckets"), dict)
            and _safe_int(raw.get("bucket_seconds"), _SMS_STATS_BUCKET_SECONDS)
            == _SMS_STATS_BUCKET_SECONDS
            and _safe_int(raw.get("retention_seconds"), _SMS_STATS_RETENTION_SECONDS)
            == _SMS_STATS_RETENTION_SECONDS
        )
        if is_v2:
            for bucket, rows in raw["buckets"].items():
                if not isinstance(rows, dict):
                    continue
                clean_rows = {}
                for key, source in rows.items():
                    if not isinstance(source, dict):
                        continue
                    clean_rows[str(key)] = {
                        field: max(0, _safe_int(source.get(field), 0))
                        for field in ("success", "failure", "rt_success", "rt_failure")
                    }
                    clean_rows[str(key)]["updated_at"] = _safe_float(
                        source.get("updated_at"), 0.0
                    )
                if clean_rows:
                    buckets[str(bucket)] = clean_rows
    except Exception:
        pass

    # v1 只有累计值，无法可靠拆分出最近 24h。按用户要求直接从空窗口开始。
    _SMS_STATS_BUCKETS = buckets
    _SMS_STATS = {}
    _SMS_STATS_WINDOW = None
    _refresh_stats_window_locked(force=True)
    if not is_v2:
        _write_stats_locked()
    return _SMS_STATS


def _normalise_supplier_strategy(value) -> str:
    """只允许明确的两种策略；缺失/脏配置默认成功率优先。"""
    return "balanced" if str(value or "").strip().lower() == "balanced" else "success_first"


def _normalise_sms_mode(value) -> str:
    """规范取号模式；session_race 由 registrar 启动两个完整独立会话。"""
    mode = str(value or "single").strip().lower().replace("-", "_")
    if mode in {"session_race", "dual_session", "full_race"}:
        return "session_race"
    if mode in {"split", "balanced", "round_robin", "roundrobin", "non_race", "sequential"}:
        return "split"
    if mode == "race":
        return "race"
    return "single"


def _normalise_sms_provider_key(value) -> str:
    """规范供应商别名；返回空串表示未指定，未知值原样返回供调用方报错。"""
    key = str(value or "").strip().lower().replace("-", "_")
    if key in {"smsbower", "sms_bower"}:
        return "smsbower"
    if key in {"herosms", "hero_sms"}:
        return "herosms"
    return key


def _split_platform_order(providers: list[BaseSmsProvider]) -> list[BaseSmsProvider]:
    """返回稳定的平台顺序；不使用配置中的主平台顺序作为偏置。"""
    priority = {"herosms": 0, "smsbower": 1}
    return sorted(
        list(providers),
        key=lambda provider: (
            priority.get(str(getattr(provider, "platform_key", "") or "").lower(), 2),
            str(getattr(provider, "platform_key", "") or "").lower(),
        ),
    )


def _select_split_provider(providers: list[BaseSmsProvider]) -> BaseSmsProvider:
    """原子选择下一平台，使双平台的租号尝试长期保持 1:1。

    计数按“已发起的取号尝试”增加，而不是按成功数增加。这样一个平台短暂
    失败时，下一次请求仍会轮到它，不会因为另一平台成功而永久吞掉流量。
    调用方负责在选中平台失败后串行尝试其它平台。
    """
    ordered = _split_platform_order(providers)
    if not ordered:
        raise RuntimeError("没有可调度的接码平台")
    if len(ordered) == 1:
        return ordered[0]
    platform_keys = tuple(
        str(getattr(provider, "platform_key", "") or "").strip().lower()
        for provider in ordered
    )
    state_key = "|".join(platform_keys)
    with _SMS_SPLIT_LOCK:
        state = _SMS_SPLIT_STATE.setdefault(
            state_key,
            {"counts": {key: 0 for key in platform_keys}, "cursor": 0},
        )
        counts = state.setdefault("counts", {})
        for key in platform_keys:
            counts.setdefault(key, 0)
        cursor = int(state.get("cursor") or 0) % len(ordered)
        # 先按分配数补齐落后平台；计数相同则从上次位置之后开始，形成轮转。
        chosen_index = min(
            range(len(ordered)),
            key=lambda index: (
                int(counts.get(platform_keys[index], 0)),
                (index - cursor) % len(ordered),
            ),
        )
        chosen_key = platform_keys[chosen_index]
        counts[chosen_key] = int(counts.get(chosen_key, 0)) + 1
        state["cursor"] = (chosen_index + 1) % len(ordered)
    return ordered[chosen_index]


def _record_split_attempt(providers: list[BaseSmsProvider], provider: BaseSmsProvider) -> None:
    """记录分流过程中的串行回退尝试，保持后续配额仍接近 1:1。"""
    ordered = _split_platform_order(providers)
    keys = tuple(
        str(getattr(item, "platform_key", "") or "").strip().lower() for item in ordered
    )
    key = str(getattr(provider, "platform_key", "") or "").strip().lower()
    if not keys or key not in keys:
        return
    state_key = "|".join(keys)
    with _SMS_SPLIT_LOCK:
        state = _SMS_SPLIT_STATE.setdefault(
            state_key, {"counts": {item: 0 for item in keys}, "cursor": 0}
        )
        counts = state.setdefault("counts", {})
        for item in keys:
            counts.setdefault(item, 0)
        counts[key] = int(counts.get(key, 0)) + 1


def _sum_stat_rows(rows) -> dict[str, int]:
    totals = {"success": 0, "failure": 0, "rt_success": 0, "rt_failure": 0}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in totals:
            try:
                totals[field] += max(0, int(row.get(field) or 0))
            except (TypeError, ValueError):
                continue
    return totals


def _stats_snapshot(platform: str, country: str, provider_id: int = 0) -> tuple[dict, dict]:
    """读取供应商/国家统计；provider_id=0 时汇总该国家所有号段。"""
    platform_key = str(platform or "smsbower").strip().lower()
    country_key = str(country or "").strip()
    exact = _stats_key(platform_key, country_key, provider_id)
    prefix = f"{platform_key}:{country_key}:"
    with _SMS_STATS_LOCK:
        stats = _load_stats()
        batch_stats = _SMS_BATCH_STATS
        if provider_id:
            history_rows = [stats.get(exact, {})]
            batch_rows = [batch_stats.get(exact, {})]
        else:
            history_rows = [row for key, row in stats.items() if str(key).startswith(prefix)]
            batch_rows = [row for key, row in batch_stats.items() if str(key).startswith(prefix)]
        return _sum_stat_rows(history_rows), _sum_stat_rows(batch_rows)


def _record_stats(
    platform: str, country: str, provider_id: int, *, success: bool, stage: str = "sms"
) -> None:
    global _SMS_STATS_BUCKETS, _SMS_STATS_WINDOW, _SMS_STATS_VERSION
    key = _stats_key(platform, country, provider_id)
    prefix = "rt_" if stage == "rt" else ""
    field = prefix + ("success" if success else "failure")
    with _SMS_STATS_LOCK:
        _SMS_STATS_VERSION += 1
        stats = _load_stats()
        now = time.time()
        if _SMS_STATS_BUCKETS is None:
            # 测试/嵌入调用可能直接注入内存统计；从下一条事件开始启用 v2 时间桶。
            _SMS_STATS_BUCKETS = {}
            _SMS_STATS_WINDOW = (
                _stats_min_bucket(now, _SMS_STATS_RETENTION_SECONDS),
                _stats_min_bucket(now, _SMS_STATS_RECENT_SECONDS),
            )
        bucket_key = str(_stats_bucket_floor(now))
        bucket = _SMS_STATS_BUCKETS.setdefault(bucket_key, {})
        bucket_row = bucket.setdefault(
            key, {"success": 0, "failure": 0, "rt_success": 0, "rt_failure": 0}
        )
        bucket_row[field] = _safe_int(bucket_row.get(field), 0) + 1
        bucket_row["updated_at"] = now

        row = stats.setdefault(
            key, {"success": 0, "failure": 0, "rt_success": 0, "rt_failure": 0}
        )
        row[field] = _safe_int(row.get(field), 0) + 1
        row["updated_at"] = now
        batch_row = _SMS_BATCH_STATS.setdefault(
            key, {"success": 0, "failure": 0, "rt_success": 0, "rt_failure": 0}
        )
        batch_row[field] = _safe_int(batch_row.get(field), 0) + 1
        batch_row["updated_at"] = now
        _write_stats_locked()


def _candidate_samples(platform: str, country: str, provider_id: int) -> int:
    history, batch = _stats_snapshot(platform, country, provider_id)
    return sum(history.values()) + sum(batch.values())


def _country_quality_map(platform: str) -> dict[str, tuple[float, int]]:
    """一次扫描生成整个平台的国家 SMS 成功率，供并发选号复用。

    结果按统计版本号缓存：600 并发下每个 worker 每次换号都要用它排序，
    而全表聚合是 O(统计条目数) 的纯 CPU 活，重算会把 GIL 占死。
    """
    platform_key = str(platform or "smsbower").strip().lower()
    with _SMS_QUALITY_LOCK:
        cached = _SMS_QUALITY_CACHE.get(platform_key)
        if cached is not None and cached[0] == _SMS_STATS_VERSION:
            return cached[1]
    prefix = f"{platform_key}:"

    def _aggregate(items) -> dict[str, list[int]]:
        totals: dict[str, list[int]] = {}
        for raw_key, row in items:
            key = str(raw_key)
            if not key.startswith(prefix) or not isinstance(row, dict):
                continue
            country_and_provider = key[len(prefix):]
            if ":" not in country_and_provider:
                continue
            country = country_and_provider.rsplit(":", 1)[0]
            bucket = totals.setdefault(country, [0, 0])
            bucket[0] += max(0, _safe_int(row.get("success"), 0))
            bucket[1] += max(0, _safe_int(row.get("failure"), 0))
        return totals

    with _SMS_STATS_LOCK:
        version = _SMS_STATS_VERSION
        history_totals = _aggregate(list(_load_stats().items()))
        batch_totals = _aggregate(list(_SMS_BATCH_STATS.items()))
    quality = {}
    for country in history_totals.keys() | batch_totals.keys():
        successes, failures = history_totals.get(country, [0, 0])
        samples = successes + failures
        if samples <= 0:
            successes, failures = batch_totals.get(country, [0, 0])
            samples = successes + failures
        quality[country] = ((successes / samples, samples) if samples else (0.5, 0))
    with _SMS_QUALITY_LOCK:
        _SMS_QUALITY_CACHE[platform_key] = (version, quality)
    return quality


def _country_quality(platform: str, country: str) -> tuple[float, int]:
    """返回国家历史 SMS 成功率和样本数；没有历史时返回中性先验。"""
    return _country_quality_map(platform).get(str(country or "").strip(), (0.5, 0))


def _rank_country_rows(platform: str, rows: list[dict]) -> list[dict]:
    """把有充分样本的好国家置前，把明确低成功率国家置后。"""
    ranked = []
    quality_by_country = _country_quality_map(platform)
    for index, original in enumerate(rows or []):
        row = dict(original)
        country = str(row.get("country") or "").strip()
        rate, samples = quality_by_country.get(country, (0.5, 0))
        if samples >= _SMS_MIN_COUNTRY_SAMPLES:
            tier = 0 if rate >= _SMS_BAD_COUNTRY_RATE else 2
        else:
            tier = 1
        row["history_success_rate"] = round(rate, 4) if samples else None
        row["history_samples"] = samples
        row["quality_tier"] = tier
        score = _safe_float(row.get("score"), float("inf"))
        count = _safe_int(row.get("count"), 0)
        # Stable index keeps provider/API order for candidates with no evidence.
        ranked.append((tier, -rate, score, -count, index, row))
    ranked.sort(key=lambda item: item[:-1])
    return [item[-1] for item in ranked]


def _rank_country_ids(platform: str, countries: list[str]) -> list[str]:
    """按历史质量重排国家 ID，同时去重并保持无统计项的原始顺序。"""
    seen = set()
    unique = []
    for value in countries or []:
        cid = str(value or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(cid)
    rows = _rank_country_rows(
        platform,
        [{"country": cid, "score": index, "count": 0} for index, cid in enumerate(unique)],
    )
    return [str(row["country"]) for row in rows]


def _rotate_country_ids(platform: str, countries: list[str]) -> list[str]:
    """Spread concurrent acquisitions across the best currently-equivalent countries."""
    ranked_rows = _rank_country_rows(
        platform,
        [{"country": cid, "score": index, "count": 0} for index, cid in enumerate(countries)],
    )
    if len(ranked_rows) < 2:
        return [str(row["country"]) for row in ranked_rows]

    best_tier = int(ranked_rows[0].get("quality_tier") or 0)
    leading = [row for row in ranked_rows if int(row.get("quality_tier") or 0) == best_tier]
    # 同档内也要按成功率收窄：档线是 15%，不收窄的话 90% 的国家会和 16% 的
    # 国家轮着用，等于把一半的取号送进注定超时的号段。
    if leading:
        top_rate = max(_safe_float(row.get("history_success_rate"), 0.0) or 0.0
                       for row in leading)
        near_best = [
            row for row in leading
            if row.get("history_success_rate") is None
            or _safe_float(row.get("history_success_rate"), 0.0)
            >= top_rate - _SMS_ROTATE_RATE_TOLERANCE
        ]
        if near_best:
            trailing = [row for row in ranked_rows if row not in near_best]
            leading = near_best
        else:
            trailing = ranked_rows[len(leading):]
    else:
        trailing = ranked_rows[len(leading):]
    if len(leading) < 2:
        return [str(row["country"]) for row in ranked_rows]

    key = str(platform or "smsbower").strip().lower()
    with _SMS_COUNTRY_CURSOR_LOCK:
        offset = int(_SMS_COUNTRY_CURSORS.get(key, 0)) % len(leading)
        _SMS_COUNTRY_CURSORS[key] = offset + 1
    rotated = leading[offset:] + leading[:offset] + trailing
    return [str(row["country"]) for row in rotated]


def _inflight_count(platform: str, country: str, provider_id: int) -> int:
    with _SMS_INFLIGHT_LOCK:
        return int(_SMS_INFLIGHT.get(_stats_key(platform, country, provider_id)) or 0)


def _reserve_candidate(
    platform: str, country: str, provider_id: int, capacity: int = 0
) -> str:
    key = _stats_key(platform, country, provider_id)
    with _SMS_INFLIGHT_LOCK:
        current = int(_SMS_INFLIGHT.get(key) or 0)
        if capacity > 0 and current >= capacity:
            return ""
        _SMS_INFLIGHT[key] = current + 1
    return key


def _release_candidate(key: str) -> None:
    if not key:
        return
    with _SMS_INFLIGHT_LOCK:
        remaining = int(_SMS_INFLIGHT.get(key) or 0) - 1
        if remaining > 0:
            _SMS_INFLIGHT[key] = remaining
        else:
            _SMS_INFLIGHT.pop(key, None)


def _candidate_score(
    platform: str,
    country: str,
    provider_id: int,
    price: float,
    count: int,
    strategy: str = SMS_DEFAULT_SUPPLIER_STRATEGY,
) -> float:
    """动态路由积分；越低越优，支持成本综合和完全忽略价格两种模式。"""
    strategy = _normalise_supplier_strategy(strategy)
    row, batch = _stats_snapshot(platform, country, provider_id)
    successes = int(row.get("success") or 0)
    failures = int(row.get("failure") or 0)
    batch_successes = int(batch.get("success") or 0)
    batch_failures = int(batch.get("failure") or 0)
    rt_successes = int(row.get("rt_success") or 0)
    rt_failures = int(row.get("rt_failure") or 0)
    batch_rt_successes = int(batch.get("rt_success") or 0)
    batch_rt_failures = int(batch.get("rt_failure") or 0)
    # Beta(1, 1) 平滑；批次实时表现 3x，最终 RT 质量高于仅收到短信。
    batch_rate = (batch_successes + 1) / (batch_successes + batch_failures + 2)
    history_rate = (successes + 1) / (successes + failures + 2)
    sms_quality = (batch_rate * 3 + history_rate) / 4
    rt_batch_rate = (batch_rt_successes + 1) / (batch_rt_successes + batch_rt_failures + 2)
    rt_history_rate = (rt_successes + 1) / (rt_successes + rt_failures + 2)
    rt_quality = (rt_batch_rate * 3 + rt_history_rate) / 4
    sms_samples = successes + failures + batch_successes + batch_failures
    rt_samples = rt_successes + rt_failures + batch_rt_successes + batch_rt_failures
    # RT 只覆盖最终完成的少数任务，不能让一个很小的、幸存者偏差明显的
    # RT 样本推翻大量“是否收到 SMS”的证据（线上 country=4 就是这种情况）。
    rt_weight = min(0.65, rt_samples / max(1, sms_samples)) if rt_samples else 0.0
    quality = sms_quality * (1.0 - rt_weight) + rt_quality * rt_weight

    inflight = _inflight_count(platform, country, provider_id)
    capacity = max(1, int(count or 0))
    inflight_pressure = 1 + (6.0 * inflight / capacity)
    stock_penalty = 1 + 4 / max(1, int(count or 0))
    samples = successes + failures + rt_successes + rt_failures
    exploration_bonus = 1 + min(0.5, 2 / ((samples + 1) ** 0.5))
    inventory_count = int(count or 0)
    price_value = max(0.0, _safe_float(price, 0.0))
    if strategy == "success_first":
        price_factor = 1.0
    else:
        # balanced 仍可偏好便宜号，但价格影响封顶在 1.5x，不能盖过质量差异。
        price_factor = 1.0 + min(0.5, price_value / _SMS_PRICE_REFERENCE * 0.5)
    if inventory_count <= 0 and price_value <= 0:
        # 目录缺失/旧接口返回的未知候选不能因 price=0 被判为最优。
        price_factor = max(price_factor, 1.5)
        stock_penalty *= 2.0
    return price_factor * stock_penalty * inflight_pressure / max(quality * exploration_bonus, 0.000001)


def _adaptive_timeout(platform: str, country: str, provider_id: int, base: float = 45.0) -> int:
    """按本批次表现给好号段更多等待时间，差号段更快换号。"""
    key = _stats_key(platform, country, provider_id)
    with _SMS_STATS_LOCK:
        _load_stats()
        row = dict(_SMS_BATCH_STATS.get(key, {}))
    successes = int(row.get("success") or 0)
    failures = int(row.get("failure") or 0)
    total = successes + failures
    if total == 0:
        return max(20, round(base * 0.78))
    rate = successes / total
    if rate >= 0.5:
        return max(20, round(base * 1.22))
    if rate >= 0.2:
        return max(20, round(base * 0.62))
    return max(20, round(base * 0.44))


def _cooldown_key(platform: str, country: str, provider_id: int = 0) -> str:
    return _stats_key(platform, country, provider_id)


def _is_cooled_down(platform: str, country: str, provider_id: int = 0) -> bool:
    key = _cooldown_key(platform, country, provider_id)
    with _SMS_COOLDOWN_LOCK:
        until = _SMS_COOLDOWNS.get(key, 0)
        if until <= time.time():
            _SMS_COOLDOWNS.pop(key, None)
            return False
        return True


def _cooldown_remaining(platform: str, country: str, provider_id: int = 0) -> float:
    key = _cooldown_key(platform, country, provider_id)
    with _SMS_COOLDOWN_LOCK:
        return max(0.0, float(_SMS_COOLDOWNS.get(key, 0.0)) - time.time())


def _cool_down(platform: str, country: str, provider_id: int = 0, seconds: int = 60) -> None:
    with _SMS_COOLDOWN_LOCK:
        _SMS_COOLDOWNS[_cooldown_key(platform, country, provider_id)] = time.time() + max(1, seconds)


def _is_platform_banned(platform: str) -> bool:
    key = str(platform or "smsbower").strip().lower()
    with _SMS_BAN_LOCK:
        _load_persisted_bans()
        until = _SMS_BANS.get(key, 0.0)
        if until <= time.time():
            # 只有真的存在过期条目才需要落盘；没有封禁是常态，
            # 无条件 _save_persisted_bans() 等于每次取号都写一次文件。
            if key in _SMS_BANS:
                _SMS_BANS.pop(key, None)
                _save_persisted_bans()
            return False
        return True


def _platform_ban_remaining(platform: str) -> float:
    key = str(platform or "smsbower").strip().lower()
    with _SMS_BAN_LOCK:
        _load_persisted_bans()
        return max(0.0, float(_SMS_BANS.get(key, 0.0)) - time.time())


def _mark_platform_banned(platform: str, reason: str = "") -> None:
    key = str(platform or "smsbower").strip().lower()
    until = time.time() + _SMS_BAN_SECONDS
    for token in str(reason or "").split(":"):
        if token.isdigit() and len(token) >= 10:
            candidate = float(token)
            if candidate > time.time():
                until = max(until, candidate + 60)
    with _SMS_BAN_LOCK:
        _SMS_BANS[key] = max(_SMS_BANS.get(key, 0.0), until)
        _save_persisted_bans()
    logger.error("%s 供应商返回封禁，暂停取号 %ss: %s", key, _SMS_BAN_SECONDS, str(reason)[:240])


def _inventory_unavailable(error: Exception | str) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in ("no_numbers", "no numbers", "无可用号码", "banned"))


class SmsTemporarilyUnavailable(RuntimeError):
    """供应商暂时无容量；调用方应按 retry_after 退避，不能立即空转。"""

    def __init__(self, message: str, retry_after: float = 5.0):
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after or 1.0))


def _earliest_retry_after(current: float, candidate: float) -> float:
    """Keep the shortest positive wait until any candidate becomes usable."""
    value = max(0.0, float(candidate or 0.0))
    if value <= 0:
        return current
    return value if current <= 0 else min(current, value)


def _phone_rejected_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(marker in text for marker in (
        "phone_number_already_in_use", "already_in_use", "already in use", "already_taken",
        "phone_already_verified", "already_verified", "disallowed_phone", "invalid_phone",
        "invalid phone", "phone_number_invalid", "blocked_phone", "phone_number_blocked",
        "suspicious", "not valid", "voip",
    ))


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _hero_price_priority_key(
    row: dict, *, threshold: float = 0, min_stock: int = 0
) -> tuple:
    """HeroSMS routing key: threshold bucket, then the actual lowest price."""
    price = _safe_float(row.get("price"), float("inf"))
    if price <= 0:
        price = float("inf")
    threshold_bucket = 0 if threshold <= 0 or price <= threshold + 1e-9 else 1
    count = _safe_int(row.get("count"), 0)
    stock_bucket = 0 if count >= max(1, min_stock) else (1 if count > 0 else 2)
    history_rate = _safe_float(row.get("history_success_rate"), -1.0)
    return (
        threshold_bucket,
        price,
        stock_bucket,
        int(row.get("quality_tier") or 0),
        -history_rate,
        -count,
    )


def _project_cache_dir() -> Path:
    root = Path(__file__).resolve().parent
    cache = root / "data"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _sms_cache_file(platform: str) -> Path:
    safe_platform = "".join(ch for ch in str(platform or "smsbower").lower() if ch.isalnum() or ch in "_-")
    return _project_cache_dir() / f".sms_phone_cache_{safe_platform or 'smsbower'}.json"


def _sms_stats_file() -> Path:
    return _project_cache_dir() / ".sms_provider_stats.json"


def _sms_ban_file() -> Path:
    return _project_cache_dir() / ".sms_provider_bans.json"


def _load_persisted_bans() -> None:
    # 用"上次读盘时间"判断，不能用 `if _SMS_BANS: return`：没有任何封禁时
    # 字典恒为空，短路永远不成立，于是每次取号都在 _SMS_BAN_LOCK 里读一次
    # 磁盘 + 解析 JSON。而没有封禁恰恰是常态 —— 线上 py-spy 抓到 87/636 个
    # 线程堵在这条路径上。
    global _SMS_BANS_LOADED_AT
    now = time.time()
    if now - _SMS_BANS_LOADED_AT < _SMS_BANS_RELOAD_TTL:
        return
    _SMS_BANS_LOADED_AT = now
    try:
        payload = json.loads(_sms_ban_file().read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            for platform, until in payload.items():
                if float(until or 0) > now:
                    _SMS_BANS[str(platform)] = float(until)
    except Exception:
        return


def _save_persisted_bans() -> None:
    try:
        now = time.time()
        _sms_ban_file().write_text(
            json.dumps({k: v for k, v in _SMS_BANS.items() if v > now}),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("保存 SMS 平台封禁状态失败", exc_info=True)


def _sms_catalog_file(platform: str) -> Path:
    safe_platform = "".join(
        ch for ch in str(platform or "smsbower").lower() if ch.isalnum() or ch in "_-"
    )
    return _project_cache_dir() / f".sms_supplier_catalog_{safe_platform or 'smsbower'}.json"


def _sms_blacklist_file() -> Path:
    return _project_cache_dir() / ".sms_phone_blacklist.txt"


def _track_sms_activation(
    platform: str, activation_id: str, phone_number: str, acquired_at: float
) -> None:
    """Best-effort bridge to the WebUI's durable cleanup queue."""
    try:
        from webui import db as webui_db

        webui_db.track_sms_activation(
            platform,
            activation_id,
            phone_number=phone_number,
            acquired_at=acquired_at,
            lifetime_seconds=SMS_PHONE_LIFETIME,
        )
    except Exception as exc:
        logger.warning(
            "SMS 租号清理记录写入失败 platform=%s activation_id=%s: %s",
            platform,
            activation_id,
            exc,
        )


def _queue_sms_activation_cancel(
    platform: str,
    activation_id: str,
    *,
    phone_number: str = "",
    acquired_at: Optional[float] = None,
    error: str = "",
) -> None:
    try:
        from webui import db as webui_db

        acquired = float(acquired_at or time.time())
        not_before = time.time()
        if str(platform or "").strip().lower() == "herosms":
            not_before = max(not_before, acquired + HERO_CANCEL_MIN_AGE_SECONDS)
        webui_db.queue_sms_activation_cancel(
            platform,
            activation_id,
            phone_number=phone_number,
            acquired_at=acquired,
            not_before=not_before,
            error=error,
        )
    except Exception as exc:
        logger.warning(
            "SMS 取消任务写入失败 platform=%s activation_id=%s: %s",
            platform,
            activation_id,
            exc,
        )


def _complete_sms_activation_cleanup(platform: str, activation_id: str) -> None:
    try:
        from webui import db as webui_db

        webui_db.complete_sms_activation_cleanup(platform, activation_id)
    except Exception as exc:
        logger.warning(
            "SMS 清理记录完成失败 platform=%s activation_id=%s: %s",
            platform,
            activation_id,
            exc,
        )


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _ensure_blacklist_loaded() -> None:
    global _SMS_BLACKLIST_LOADED
    if _SMS_BLACKLIST_LOADED:
        return
    try:
        for line in _sms_blacklist_file().read_text(encoding="utf-8").splitlines():
            number = _normalize_phone(line)
            if number:
                _SMS_BLACKLIST.add(number)
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("加载 SMS 号码黑名单失败", exc_info=True)
    _SMS_BLACKLIST_LOADED = True


def _is_phone_blacklisted(phone: str) -> bool:
    with _SMS_BLACKLIST_LOCK:
        _ensure_blacklist_loaded()
        return _normalize_phone(phone) in _SMS_BLACKLIST


def _add_phone_blacklist(phone: str) -> bool:
    number = _normalize_phone(phone)
    if not number:
        return False
    with _SMS_BLACKLIST_LOCK:
        _ensure_blacklist_loaded()
        if number in _SMS_BLACKLIST:
            return False
        _SMS_BLACKLIST.add(number)
        try:
            with _sms_blacklist_file().open("a", encoding="utf-8") as handle:
                handle.write(number + "\n")
        except Exception:
            logger.warning("保存 SMS 号码黑名单失败", exc_info=True)
        return True


def _parse_sms_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _make_sms_candidate(activation_id: str, source: str, code) -> Optional[dict]:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": hashlib.sha256(
            f"{activation_id}:{code}".encode("utf-8")
        ).hexdigest(),
    }


class SmsBowerProvider(BaseSmsProvider):
    """sms-activate 协议系 provider（SmsBower / HeroSMS 共用）。"""

    DEFAULT_BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    auto_report_success_on_code = False  # 等业务侧确认才报成功（便于号码复用）

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        platform_key: str = "smsbower",
        default_service: str = SMS_DEFAULT_SERVICE,
        default_country: str = SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        proxy: Optional[str] = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
        supplier_strategy: str = SMS_DEFAULT_SUPPLIER_STRATEGY,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip() or self.DEFAULT_BASE_URL
        self.platform_key = str(platform_key or "smsbower").strip().lower()
        self.default_service = str(default_service or SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self._proxy = (proxy or "").strip() or None
        self._proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self.supplier_strategy = _normalise_supplier_strategy(supplier_strategy)
        self._resend_callback: Optional[Callable[[], None]] = None
        self._should_stop_callback: Optional[Callable[[], bool]] = None
        self.last_code_result: Optional[dict] = None
        self.last_cancel_error = ""
        self.current_activation: Optional[SmsActivation] = None
        self._used_codes: set[str] = set()
        self._ranked_providers_by_country: dict[str, list[dict]] = {}

    def _score_row(self, row: dict) -> float:
        return _candidate_score(
            self.platform_key,
            str(row.get("country") or ""),
            int(row.get("provider_id") or 0),
            float(row.get("price") or 0),
            int(row.get("count") or 0),
            self.supplier_strategy,
        )

    def _apply_provider_catalog(self, provider_rows: list[dict]) -> list[dict]:
        """把供应商目录压成候选国家列表。

        逐行打分 + 排序是纯 CPU 活，几千行的目录在 600 并发下每人算一遍会把
        GIL 占满（线上 py-spy 抓到 542/1235 个线程同时卡在这条路径）。评分只
        依赖统计版本和 inflight 分布，秒级内结果几乎不变，所以按 TTL 共享。
        """
        cache_key = (
            f"{self.platform_key}:{self.max_price}:{self.supplier_strategy}:"
            f"{len(provider_rows)}"
        )
        now = time.time()
        with _SMS_RANKED_LOCK:
            cached = _SMS_RANKED_CACHE.get(cache_key)
            if (
                cached is not None
                and cached["stats_version"] == _SMS_STATS_VERSION
                and now - cached["saved_at"] < _SMS_RANKED_TTL
            ):
                self._ranked_providers_by_country = cached["by_country"]
                return [dict(row) for row in cached["rows"]]
            stats_version = _SMS_STATS_VERSION
        grouped: dict[str, list[dict]] = {}
        for original in provider_rows:
            row = dict(original)
            if self.max_price > 0:
                row_price = _safe_float(row.get("price"), 0)
                if row_price > self.max_price + 1e-9:
                    continue
            row["score"] = self._score_row(row)
            grouped.setdefault(str(row.get("country") or ""), []).append(row)
        rows = []
        by_country: dict[str, list[dict]] = {}
        for cid, candidates in grouped.items():
            if self.platform_key == "herosms":
                candidates.sort(
                    key=lambda row: (
                        _safe_float(row.get("price"), float("inf")),
                        row["score"],
                        -int(row.get("count") or 0),
                    )
                )
            else:
                candidates.sort(key=lambda row: (row["score"], -int(row.get("count") or 0)))
            by_country[cid] = candidates
            best = candidates[0]
            rows.append({
                "country": cid,
                "price": best["price"],
                "count": sum(int(row.get("count") or 0) for row in candidates),
                "score": best["score"],
                "provider_id": best["provider_id"],
                "provider_count": len(candidates),
            })
        ranked = _rank_country_rows(self.platform_key, rows)
        if self.platform_key == "herosms":
            ranked.sort(
                key=lambda row: _hero_price_priority_key(
                    row, threshold=self.max_price if self.max_price > 0 else 0
                )
            )
        self._ranked_providers_by_country = by_country
        with _SMS_RANKED_LOCK:
            _SMS_RANKED_CACHE[cache_key] = {
                "saved_at": now,
                "stats_version": stats_version,
                "rows": [dict(row) for row in ranked],
                "by_country": by_country,
            }
        return ranked

    def _save_provider_catalog(self, service: str, provider_rows: list[dict]) -> None:
        payload = {
            "service": service,
            "saved_at": time.time(),
            "rows": [
                {k: row.get(k) for k in (
                    "country", "provider_id", "provider_name", "price", "count"
                )}
                for row in provider_rows
            ],
        }
        try:
            _sms_catalog_file(self.platform_key).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            logger.debug("保存供应商目录失败", exc_info=True)

    def _load_provider_catalog(self, service: str, max_age: int = 21600) -> list[dict]:
        # 磁盘目录 150KB / 1700 行，解析一次不便宜。它是内存缓存过期时的回退路径，
        # 几百个 worker 会同时走到这儿，所以解析结果按平台+服务再缓存一层。
        disk_key = f"{self.platform_key}:{service}"
        now = time.time()
        with _SMS_CATALOG_LOCK:
            hit = _SMS_CATALOG_DISK_CACHE.get(disk_key)
            if hit is not None and now - hit[0] < _SMS_CATALOG_DISK_TTL:
                return [dict(row) for row in hit[1]]
        try:
            payload = json.loads(_sms_catalog_file(self.platform_key).read_text(encoding="utf-8"))
            if str(payload.get("service") or "") != service:
                return []
            if time.time() - float(payload.get("saved_at") or 0) > max_age:
                return []
            rows = payload.get("rows") or []
            parsed = [dict(row) for row in rows if isinstance(row, dict)]
        except Exception:
            return []
        with _SMS_CATALOG_LOCK:
            _SMS_CATALOG_DISK_CACHE[disk_key] = (now, parsed)
        return [dict(row) for row in parsed]

    # ---- HTTP ----

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        action = str(params.get("action") or "")
        if action in {"getNumber", "getNumberV2"} and _is_platform_banned(self.platform_key):
            raise RuntimeError(f"{self.platform_key} 供应商封禁冷却中")
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        last_error = None
        for attempt in range(3):
            try:
                resp = requests.get(self.base_url, params=payload, timeout=timeout, proxies=self._proxies)
                if resp.status_code == 403 and str(payload.get("action") or "") == "getNumber":
                    _mark_platform_banned(self.platform_key, "HTTP 403")
                resp.raise_for_status()
                return resp
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"{self.platform_key} 请求失败: {last_error}")

    # ---- 余额 / 价格 / 国家 ----

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"SmsBower getBalance 失败: {text}")

    def get_prices(self, service: Optional[str] = None, country=None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("SmsBower getPrices 返回结构异常")

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        """按预估成功成本返回国家列表，并保留每国的供应商优先级。"""
        service_code = str(service or self.default_service or SMS_DEFAULT_SERVICE).strip()
        cache_key = f"{self.platform_key}:{service_code}:{self.max_price}:{self.supplier_strategy}"
        # 锁内只取行，转换/打分放到锁外做：_apply_provider_catalog 是纯 CPU，
        # 在锁里跑会让所有命中缓存的 worker 也排成一列。
        with _SMS_CATALOG_LOCK:
            cached = _SMS_CATALOG_CACHE.get(cache_key)
        if cached and time.time() - float(cached.get("saved_at") or 0) < _SMS_CATALOG_TTL:
            return self._apply_provider_catalog(cached["rows"])
        # 刷新单飞按 cache_key 隔离，且拿到锁后重新检查缓存：并发 worker 里
        # 只有第一个真正发请求。
        fetch_lock = _catalog_fetch_lock(cache_key)
        if not fetch_lock.acquire(blocking=False):
            # 已经有人在刷新了。上游目录接口偶尔要几十秒，让另外几百个 worker
            # 在这儿干等就是把上一版的串行瓶颈原样搬了回来（py-spy 实测 185/636
            # 个线程堵在这一行）。目录是小时级才变的数据，过期几十秒完全能用，
            # 所以后来者直接拿旧值继续取号，把刷新留给持锁的那一个。
            if cached:
                return self._apply_provider_catalog(cached["rows"])
            stale = self._load_provider_catalog(service_code)
            if stale:
                return self._apply_provider_catalog(stale)
            # 冷启动：这个平台还没有任何可用目录，只能等持锁的那个刷新出来。
            # 但不能无限期等 —— 线上 herosms 没有磁盘目录、上游又拿不到数据，
            # 224/635 个线程就这么钉死在这一行，把 split 模式的另一半算力废掉了。
            # 等一小会儿没等到就当这个平台暂时没货，让 worker 去别的平台取号。
            if not fetch_lock.acquire(timeout=_SMS_CATALOG_COLD_WAIT):
                raise RuntimeError(
                    f"{self.platform_key} 目录冷启动刷新超时（{_SMS_CATALOG_COLD_WAIT}s）"
                )
            try:
                with _SMS_CATALOG_LOCK:
                    cached = _SMS_CATALOG_CACHE.get(cache_key)
                if cached and time.time() - float(cached.get("saved_at") or 0) < _SMS_CATALOG_TTL:
                    return self._apply_provider_catalog(cached["rows"])
                return self._refresh_top_countries(service_code, cache_key)
            finally:
                fetch_lock.release()
        try:
            with _SMS_CATALOG_LOCK:
                cached = _SMS_CATALOG_CACHE.get(cache_key)
            if cached and time.time() - float(cached.get("saved_at") or 0) < _SMS_CATALOG_TTL:
                return self._apply_provider_catalog(cached["rows"])
            return self._refresh_top_countries(service_code, cache_key)
        finally:
            fetch_lock.release()

    def _commit_catalog(self, service_code: str, cache_key: str, rows: list[dict]) -> list[dict]:
        """把刷新出来的目录写进内存缓存 + 磁盘，再返回排好序的候选国家。

        以前只有 getPricesV3 那条分支会落缓存，兜底分支只是 return。对不支持
        v3 的平台（herosms 的 getPricesV3 直接 404）来说，这意味着内存缓存和
        磁盘目录永远是空的：每个 worker 都从冷启动走一遍，撞上 8s 超时后被
        _acquire_split 跳过，1:1 分流实际退化成 1:0，只有 smsbower 在干活。
        落缓存跟目录是从哪个接口来的无关，所以提取成公共步骤。
        """
        self._save_provider_catalog(service_code, rows)
        with _SMS_CATALOG_LOCK:
            _SMS_CATALOG_CACHE[cache_key] = {
                "saved_at": time.time(), "rows": [dict(row) for row in rows]
            }
            # 刚写过盘，磁盘解析缓存立即失效，避免回退路径读到上一版。
            _SMS_CATALOG_DISK_CACHE.pop(f"{self.platform_key}:{service_code}", None)
        return self._apply_provider_catalog(rows)

    def _refresh_top_countries(self, service_code: str, cache_key: str) -> list[dict]:
        # 优先使用 v3 的供应商级价格，取号时通过 providerIds 固定到对应号段。
        try:
            data = self._request({"action": "getPricesV3", "service": service_code}).json()
            provider_rows = self._parse_provider_prices(data, service_code)
            if provider_rows:
                return self._commit_catalog(service_code, cache_key, provider_rows)
        except Exception as exc:
            logger.debug("%s getPricesV3 不可用: %s", self.platform_key, exc)

        cached_provider_rows = self._load_provider_catalog(service_code)
        if cached_provider_rows:
            logger.info("%s 使用最近成功的供应商目录继续 providerIds 路由", self.platform_key)
            return self._apply_provider_catalog(cached_provider_rows)

        # 策略1：使用专用排名 API
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries(data)
                if rows:
                    for row in rows:
                        row.setdefault("provider_id", 0)
                    return self._commit_catalog(service_code, cache_key, rows)
            except Exception:
                continue
        # 策略2：从 getPrices 解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc = services.get(service_code)
                if not isinstance(svc, dict):
                    continue
                price = svc.get("cost") or svc.get("price")
                count = (svc.get("count") or svc.get("physicalCount") or svc.get("qty")
                         or svc.get("available") or 0)
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    cid = str(country_id)
                    rows.append({
                        "country": cid,
                        "provider_id": 0,
                        "price": price,
                        "count": count,
                    })
            if rows:
                return self._commit_catalog(service_code, cache_key, rows)
            return []
        except Exception:
            return []

    def _parse_provider_prices(self, data, service: str) -> list[dict]:
        rows = []
        if not isinstance(data, dict):
            return rows
        for country, services in data.items():
            if not isinstance(services, dict):
                continue
            providers = services.get(service)
            if not isinstance(providers, dict):
                continue
            for provider_key, item in providers.items():
                if not isinstance(item, dict):
                    continue
                provider_id = _safe_int(
                    item.get("provider_id") or item.get("providerId") or item.get("id") or provider_key, 0
                )
                price = _safe_float(item.get("price") or item.get("cost"), 0)
                count = _safe_int(item.get("count") or item.get("physicalCount"), 0)
                cid = str(country)
                if provider_id <= 0 or price <= 0 or count <= 0:
                    continue
                rows.append({
                    "country": cid,
                    "provider_id": provider_id,
                    "provider_name": str(item.get("provider_name") or item.get("name") or ""),
                    "price": price,
                    "count": count,
                    "score": _candidate_score(
                        self.platform_key, cid, provider_id, price, count, self.supplier_strategy
                    ),
                })
        return rows

    @staticmethod
    def _parse_top_countries(data) -> list[dict]:
        rows = []
        items = data
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = value.get("price") or value.get("cost") or value.get("retail_price")
                count = (value.get("count") or value.get("physicalCount") or value.get("qty")
                         or value.get("available") or 0)
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": country_id, "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost")
                count = (item.get("count") or item.get("physicalCount") or item.get("qty")
                         or item.get("available") or 0)
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "price": price, "count": count})
        return rows

    def get_best_country(self, service: Optional[str] = None, *,
                         min_stock: int = 20, max_price: float = 0,
                         strict_whitelist: bool = False,
                         allowed_countries: Optional[list[str]] = None) -> Optional[str]:
        """自动选最优国家。

        allowed_countries 优先级最高（用户自定义 = 从这些国家里挑最便宜+库存足的）
        strict_whitelist  = True → 只从 OPENAI_SMS_COUNTRIES 选（即 52 泰国）
        都没设 → 全部国家自由选（默认；用户自行承担"OpenAI 让用 WhatsApp"的风险）
        """
        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("SmsBower get_best_country 查询失败: %s", exc)
            return None
        if not rows:
            return None

        allowed_set: Optional[set[str]] = None
        if allowed_countries:
            allowed_set = {str(c).strip() for c in allowed_countries if str(c).strip()}

        def _pick(stock_threshold: int) -> Optional[str]:
            for row in rows:
                cid = str(row.get("country") or "")
                # 优先用 user-supplied 白名单
                if allowed_set is not None:
                    if cid not in allowed_set:
                        continue
                elif strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    continue
                price = row.get("price") or 0
                count = row.get("count") or 0
                if count < stock_threshold:
                    continue
                if max_price > 0 and price > max_price:
                    continue
                # 非白名单国家 → warn 一下（不阻止）
                if not strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    logger.warning(
                        "SmsBower 自动选了非 OpenAI-SMS 白名单国家 country=%s price=%s "
                        "（OpenAI 可能让此号用 WhatsApp 验证 → 收不到 SMS）",
                        cid, price,
                    )
                return cid
            return None

        return _pick(min_stock) or _pick(1)

    # ---- 号码复用缓存 ----

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "api_key_hash": _hash_secret(self.api_key),
            "platform": self.platform_key,
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> Optional[dict]:
        cache = _SMS_CACHE.get(self.platform_key)
        if cache is None:
            path = _sms_cache_file(self.platform_key)
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        identity = self._cache_identity(service, country)
        if any(str(cache.get(k) or "") != str(v) for k, v in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
            self._save_cache(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        _SMS_CACHE[self.platform_key] = cache
        return cache

    def _save_cache(self, cache: Optional[dict]) -> None:
        path = _sms_cache_file(self.platform_key)
        if cache is None:
            _SMS_CACHE.pop(self.platform_key, None)
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        _SMS_CACHE[self.platform_key] = cache
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    # ---- 租号 ----

    def _request_number_single_action(
        self, action: str, service: str, country: str, provider_id: int = 0
    ) -> dict:
        """单次调用 getNumberV2 或 getNumber（不自己 fallback，由调用方双重 for 控制）。

        借鉴 GuJumpgate：每个国家分别试 V2 / V1，而不是内部自动 fallback。
        """
        common = {"action": action, "service": service, "country": country}
        # 用户配了 max_price 才传，空 / <=0 时根本不传（让平台用默认）
        if self.max_price > 0:
            common["maxPrice"] = self.max_price
        if provider_id > 0:
            common["providerIds"] = str(provider_id)
        logger.info("%s %s: service=%s country=%s maxPrice=%s", self.platform_key,
                    action, service, country, common.get("maxPrice", "未设置"))

        try:
            resp = self._request(common)
            resp_text = resp.text.strip()
            logger.info("%s %s resp: status=%s text=%s", self.platform_key, action, resp.status_code, resp_text[:500])
            if resp_text.upper().startswith("BANNED:GLOBAL:"):
                _mark_platform_banned(self.platform_key, resp_text)
                raise RuntimeError(resp_text[:240])

            # V2 返回 JSON
            if action == "getNumberV2":
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("activationId"):
                        return data
                except ValueError:
                    pass
                raise RuntimeError(resp_text[:200] or "empty response")

            # V1 返回纯文本 ACCESS_NUMBER:id:phone
            if resp_text.startswith("ACCESS_NUMBER:"):
                parts = resp_text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                    }
            raise RuntimeError(resp_text[:200] or "empty response")
        except Exception as e:
            # 不在这里 fallback，让调用方的 for action 循环去试下个 action
            raise

    @staticmethod
    def _format_phone(info: dict) -> str:
        raw = str(info.get("phoneNumber") or "").strip()
        cc = str(info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if cc and raw.startswith(cc):
            return f"+{raw}"
        if cc:
            return f"+{cc}{raw}"
        return f"+{raw}"

    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        """租号。支持多国家候选依次尝试（按入参顺序）。

        country_candidates: 候选国家 ID 列表，按这个顺序依次尝试；空时只用 country 单个。

        借鉴 GuJumpgate: 双重 for 循环 —— 外层遍历国家，内层每个国家先试 getNumberV2，
        失败才 fallback getNumber（V1）。
        """
        self._raise_if_stopped()
        service_code = str(self.default_service or service or SMS_DEFAULT_SERVICE).strip()
        # 单一 country 兜底
        if not country_candidates:
            country_candidates = [str(country or self.default_country or SMS_DEFAULT_COUNTRY).strip()]
        # Hero 的调用方顺序已经按阈值和价格排好，不能再被历史成功率重排。
        # 其它平台继续沿用质量优先和同档轮转。
        if self.platform_key == "herosms":
            country_candidates = list(dict.fromkeys(
                str(cid or "").strip() for cid in country_candidates if str(cid or "").strip()
            ))
        else:
            country_candidates = _rotate_country_ids(
                self.platform_key, _rank_country_ids(self.platform_key, list(country_candidates))
            )

        if self.reuse_phone_to_max:
            with _SMS_CACHE_LOCK:
                for cached_country in country_candidates:
                    self._raise_if_stopped()
                    cache = self._load_cache(service_code, cached_country)
                    cache_rate, cache_samples = _country_quality(self.platform_key, cached_country)
                    if (
                        cache_samples >= _SMS_MIN_COUNTRY_SAMPLES
                        and cache_rate < _SMS_BAD_COUNTRY_RATE
                    ):
                        # 历史上明确低质量的旧复用号不能绕过国家排序继续使用。
                        continue
                    if cache and str(cache.get("country") or "") in country_candidates:
                        activation = SmsActivation(
                            activation_id=str(cache["activation_id"]),
                            phone_number=str(cache["phone_number"]),
                            country=str(cache.get("country") or cached_country),
                            metadata={
                                "reused": True,
                                "use_count": int(cache.get("use_count") or 0),
                                "provider_id": int(cache.get("provider_id") or 0),
                                "price": float(cache.get("price") or 0),
                                "platform": self.platform_key,
                                "count": 1,
                                "supplier_strategy": self.supplier_strategy,
                                "acquired_at": float(cache.get("acquired_at") or time.time()),
                            },
                        )
                        self.current_activation = activation
                        return activation

        if not self._ranked_providers_by_country:
            try:
                self.get_top_countries(service_code)
            except Exception:
                self._raise_if_stopped()
                pass

        failures: list[str] = []
        last_exc: Optional[Exception] = None
        retry_after = 0.0
        for cid in country_candidates:
            self._raise_if_stopped()
            cid = str(cid).strip()
            if not cid:
                continue
            country_cooldown = _cooldown_remaining(self.platform_key, cid)
            if country_cooldown > 0:
                retry_after = _earliest_retry_after(retry_after, country_cooldown)
                continue
            provider_rows = [dict(row) for row in self._ranked_providers_by_country.get(cid, [])]
            if not provider_rows:
                # 旧版目录/API 不返回该国家的供应商明细时仍允许尝试，但必须按未知候选处理，
                # 不能用 price=0/count=0 让它在评分中“免费且无限库存”。
                provider_rows = [{
                    "provider_id": 0,
                    "price": self.max_price if self.max_price > 0 else 0.06,
                    "count": 1,
                    "unknown_catalog": True,
                }]
            for row in provider_rows:
                row["country"] = cid
                row["score"] = self._score_row(row)
            if self.platform_key == "herosms":
                provider_rows.sort(
                    key=lambda row: (
                        _safe_float(row.get("price"), float("inf")),
                        row["score"],
                        -int(row.get("count") or 0),
                    )
                )
            else:
                provider_rows.sort(key=lambda row: (row["score"], -int(row.get("count") or 0)))
            country_inventory_empty = True
            for provider_row in provider_rows:
                self._raise_if_stopped()
                provider_id = int(provider_row.get("provider_id") or 0)
                provider_cooldown = _cooldown_remaining(self.platform_key, cid, provider_id)
                if provider_cooldown > 0:
                    retry_after = _earliest_retry_after(retry_after, provider_cooldown)
                    continue
                reservation_key = _reserve_candidate(
                    self.platform_key, cid, provider_id, int(provider_row.get("count") or 0)
                )
                if not reservation_key:
                    retry_after = _earliest_retry_after(retry_after, 1.0)
                    continue
                reservation_handed_off = False
                actions = ("getNumber",) if provider_id > 0 else ("getNumberV2", "getNumber")
                provider_inventory_empty = True
                try:
                    for action in actions:
                        self._raise_if_stopped()
                        # 命中持久化黑名单的号码立即退款并继续换号，直到拿到可用号码
                        # 或供应商明确返回无库存/外部停止信号。
                        blacklist_skip = 0
                        while True:
                            self._raise_if_stopped()
                            try:
                                info = self._request_number_single_action(
                                    action, service_code, cid, provider_id=provider_id
                                )
                                aid = str(info.get("activationId") or "")
                                phone = self._format_phone(info)
                                if not aid or not phone.strip("+"):
                                    raise RuntimeError("返回信息不完整")
                                acquired_at = time.time()
                                _track_sms_activation(
                                    self.platform_key, aid, phone, acquired_at
                                )
                                if self._stop_requested():
                                    self.cancel(aid, record_failure=False)
                                    raise RuntimeError("SMS 接码因任务停止而中止")
                                provider_inventory_empty = False
                                country_inventory_empty = False
                                if _is_phone_blacklisted(phone):
                                    self.cancel(aid, record_failure=False)
                                    failures.append(
                                        f"{cid}/{provider_id}: {action} 命中号码黑名单 {phone}，已退款"
                                    )
                                    blacklist_skip += 1
                                    if blacklist_skip >= 10:
                                        raise RuntimeError(
                                            f"{cid}/{provider_id} 连续 {blacklist_skip} 次命中号码黑名单"
                                        )
                                    continue
                                metadata = {
                                    "reused": False,
                                    "provider_id": provider_id,
                                    "price": float(info.get("activationCost") or provider_row.get("price") or 0),
                                    "count": int(provider_row.get("count") or 0),
                                    "platform": self.platform_key,
                                    "supplier_strategy": self.supplier_strategy,
                                    "selection_score": float(provider_row.get("score") or 0),
                                    "reservation_key": reservation_key,
                                    "acquired_at": acquired_at,
                                }
                                cache = {
                                    **self._cache_identity(service_code, cid),
                                    "country": cid,
                                    "activation_id": aid,
                                    "phone_number": phone,
                                    "provider_id": provider_id,
                                    "price": metadata["price"],
                                    "acquired_at": acquired_at,
                                    "use_count": 0,
                                    "used_codes": set(),
                                    "reuse_stopped": False,
                                    "stop_reason": "",
                                }
                                if self.reuse_phone_to_max:
                                    with _SMS_CACHE_LOCK:
                                        self._save_cache(cache)
                                activation = SmsActivation(aid, phone, cid, metadata)
                                self.current_activation = activation
                                reservation_handed_off = True
                                return activation
                            except Exception as exc:
                                if self._stop_requested():
                                    raise RuntimeError("SMS 接码因任务停止而中止") from exc
                                failures.append(f"{cid}/{provider_id}: {action}={str(exc)[:120]}")
                                last_exc = exc
                                if str(exc).upper().startswith("BANNED:SPECIFIC:"):
                                    _cool_down(self.platform_key, cid, provider_id, 15 * 60)
                                    retry_after = _earliest_retry_after(retry_after, 15 * 60)
                                    break
                                if _is_platform_banned(self.platform_key):
                                    raise RuntimeError(
                                        f"{self.platform_key} 供应商封禁冷却中，停止继续尝试"
                                    ) from exc
                                if _inventory_unavailable(exc):
                                    retry_after = _earliest_retry_after(
                                        retry_after, _SMS_INVENTORY_COOLDOWN_SECONDS
                                    )
                                    break
                                # 非黑名单重试场景交给 V2/V1 fallback 或下个供应商。
                                if "黑名单" not in str(exc):
                                    provider_inventory_empty = False
                                break
                finally:
                    if not reservation_handed_off:
                        _release_candidate(reservation_key)
                _cool_down(
                    self.platform_key, cid, provider_id,
                    _SMS_INVENTORY_COOLDOWN_SECONDS if provider_inventory_empty else 30,
                )
            _cool_down(
                self.platform_key, cid, 0,
                _SMS_INVENTORY_COOLDOWN_SECONDS if country_inventory_empty else 30,
            )

        detail = " | ".join(failures[-12:]) if failures else "所有候选处于冷却或容量已被占用"
        if retry_after > 0 or not failures:
            raise SmsTemporarilyUnavailable(
                f"{self.platform_key} 暂时无可用容量: {detail}", retry_after or 5.0
            ) from last_exc
        raise RuntimeError(
            f"{self.platform_key} 依次尝试 {len(country_candidates)} 个候选国家全失败: {detail}"
        ) from last_exc

    # ---- 等 code / 状态查询 ----

    def get_status(self, activation_id: str) -> dict:
        text = self._request({"action": "getStatus", "id": activation_id}).text
        return _parse_sms_status_text(text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request({"action": "getStatusV2", "id": activation_id})
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_sms_status_text(text)
        if isinstance(data, str):
            return _parse_sms_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown"}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_sms_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(activation_id, f"getStatusV2.{channel}", item.get("code"))
                if candidate:
                    return candidate
        return {"status": "wait_code"}

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 3})
            return True
        except Exception:
            return False

    def wait_for_code(self, activation_id: str, *, timeout: int = 80, poll: int = 3,
                       openai_resend_interval: int = 20,
                       openai_resend_max: int = 3) -> Optional[dict]:
        """等 SMS 验证码：每 `openai_resend_interval` 秒触发一次 OpenAI 端 resend，
        最多 `openai_resend_max` 次。超过 timeout 仍没收到 → 返回 None（由上层 cancel 换号）。
        """
        deadline = time.time() + timeout
        start = time.time()
        openai_resend_count = 0
        last_smsbower_resend = start
        used_codes = set(self._used_codes)
        if self.reuse_phone_to_max:
            with _SMS_CACHE_LOCK:
                cache = _SMS_CACHE.get(self.platform_key) or {}
                used_codes.update(cache.get("used_codes") or [])

        while time.time() < deadline:
            self._raise_if_stopped()
            for src in ("v2", "v1"):
                self._raise_if_stopped()
                try:
                    if src == "v2":
                        result = self.get_status_v2(activation_id)
                    else:
                        result = self.get_status(activation_id)
                    if result.get("status") == "cancel":
                        return None
                    if result.get("status") == "ok":
                        code = str(result.get("code") or "")
                        if code and code not in used_codes:
                            return {"status": "ok", "code": code,
                                    "sms_key": result.get("sms_key") or ""}
                except Exception as e:
                    logger.debug("SmsBower status %s 失败: %s", src, e)

            elapsed = time.time() - start
            self._raise_if_stopped()
            # OpenAI 端 resend：固定间隔触发，最多 N 次
            expected_resend_count = min(openai_resend_max, int(elapsed // openai_resend_interval))
            if expected_resend_count > openai_resend_count and self._resend_callback:
                try:
                    self._resend_callback()
                    openai_resend_count = expected_resend_count
                    logger.info(
                        "SmsBower: 已请求 OpenAI 端 resend (第 %d/%d 次, elapsed=%ds)",
                        openai_resend_count, openai_resend_max, int(elapsed),
                    )
                except Exception as e:
                    logger.warning("OpenAI resend callback 失败: %s", e)
                # 同步请求 SmsBower 端 resend
                self._raise_if_stopped()
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()
            elif time.time() - last_smsbower_resend >= openai_resend_interval:
                # 平时也间歇请求 SmsBower 端 resend，跟 OpenAI 同节奏
                self._raise_if_stopped()
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()

            sleep_until = min(deadline, time.time() + max(0.0, float(poll)))
            while time.time() < sleep_until:
                self._raise_if_stopped()
                time.sleep(min(0.2, max(0.0, sleep_until - time.time())))
        return None

    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        # ⚠️ 不再用 cache.remaining 延长 timeout：
        # 用户给的 timeout 就是真 timeout，超时就让上层换号或换 attempt。
        # （旧逻辑会被拉到 20 分钟号码生命周期，OpenAI 端 phone-otp challenge 等不了那么久）
        self._raise_if_stopped()
        effective_timeout = int(timeout)
        activation = self.current_activation
        if activation and str(activation.activation_id) == str(activation_id):
            provider_id = int((activation.metadata or {}).get("provider_id") or 0)
            adaptive = _adaptive_timeout(
                self.platform_key, activation.country, provider_id, base=45.0
            )
            effective_timeout = min(effective_timeout, adaptive)
            logger.info(
                "%s 自适应 SMS 窗口: country=%s provider=%s requested=%ss effective=%ss",
                self.platform_key, activation.country, provider_id, timeout, effective_timeout,
            )
        candidate = self.wait_for_code(activation_id, timeout=effective_timeout)
        self._raise_if_stopped()
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    # ---- 状态报告 ----

    def _record_current_result(self, activation_id: str, *, success: bool) -> None:
        activation = self.current_activation
        if not activation or str(activation.activation_id) != str(activation_id):
            return
        metadata = activation.metadata or {}
        _record_stats(
            self.platform_key,
            activation.country,
            int(metadata.get("provider_id") or 0),
            success=success,
        )

    def _release_current_reservation(self, activation_id: str) -> None:
        activation = self.current_activation
        if not activation or str(activation.activation_id) != str(activation_id):
            return
        metadata = activation.metadata or {}
        key = str(metadata.get("reservation_key") or "")
        if key and not metadata.get("reservation_released"):
            _release_candidate(key)
            metadata["reservation_released"] = True

    def cancel(self, activation_id: str, *, record_failure: bool = True) -> bool:
        activation = self.current_activation
        metadata = (activation.metadata or {}) if activation else {}
        acquired_at = _safe_float(metadata.get("acquired_at"), time.time())
        phone_number = activation.phone_number if activation else ""
        defer_hero_cancel = (
            self.platform_key == "herosms"
            and time.time() < acquired_at + HERO_CANCEL_MIN_AGE_SECONDS
        )
        # 先持久化再请求上游；进程即使在 HTTP 中途退出，后台仍能继续取消。
        _queue_sms_activation_cancel(
            self.platform_key,
            activation_id,
            phone_number=phone_number,
            acquired_at=acquired_at,
            error="等待 HeroSMS 可取消窗口" if defer_hero_cancel else "等待取消",
        )
        ok = False
        cancel_errors: list[str] = []
        if not defer_hero_cancel:
            try:
                resp = self._request({"action": "cancelActivation", "id": activation_id})
                ok = resp.status_code == 204 or "ACCESS_CANCEL" in resp.text
                if not ok:
                    cancel_errors.append(resp.text.strip()[:240] or f"HTTP {resp.status_code}")
            except Exception as exc:
                cancel_errors.append(str(exc)[:240])
                ok = False
            if not ok:
                try:
                    resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
                    ok = "ACCESS_CANCEL" in resp.text
                    if not ok:
                        cancel_errors.append(resp.text.strip()[:240] or f"HTTP {resp.status_code}")
                except Exception as exc:
                    cancel_errors.append(str(exc)[:240])
                    ok = False
        else:
            cancel_errors.append("等待 HeroSMS 可取消窗口")
            logger.info(
                "HeroSMS activation_id=%s 尚未到可取消时间，已交给后台队列",
                activation_id,
            )
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE.get(self.platform_key)
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._clear_cache()
        if record_failure:
            self._record_current_result(activation_id, success=False)
        self._release_current_reservation(activation_id)
        if ok:
            self.last_cancel_error = ""
            _complete_sms_activation_cleanup(self.platform_key, activation_id)
        elif cancel_errors and not defer_hero_cancel:
            self.last_cancel_error = " | ".join(cancel_errors)
            _queue_sms_activation_cancel(
                self.platform_key,
                activation_id,
                phone_number=phone_number,
                acquired_at=acquired_at,
                error=self.last_cancel_error,
            )
        elif cancel_errors:
            self.last_cancel_error = " | ".join(cancel_errors)
        return ok

    def report_success(self, activation_id: str) -> bool:
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE.get(self.platform_key)
            should_finish = False
            should_clear = False
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    self._used_codes.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                remaining = SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    should_finish = True
                    should_clear = True
                    cache["reuse_stopped"] = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    should_finish = True
                    cache["reuse_stopped"] = True
                elif remaining <= 30:
                    should_finish = True
                    should_clear = True
                    cache["reuse_stopped"] = True
                self._save_cache(cache)
                if should_clear:
                    self._clear_cache()
        self._record_current_result(activation_id, success=True)
        self._release_current_reservation(activation_id)
        finalized = should_finish or not (
            cache and str(cache.get("activation_id")) == str(activation_id)
        )
        if finalized:
            # 业务已经成功消费；即使 finishActivation 瞬时失败，也绝不能被
            # 后台清理任务当作失败号码再取消。
            _complete_sms_activation_cleanup(self.platform_key, activation_id)
        try:
            if finalized:
                resp = self._request({"action": "finishActivation", "id": activation_id})
                return resp.status_code in (200, 204) or "ACCESS" in resp.text
        except Exception:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 6})
                return "ACCESS" in resp.text
            except Exception:
                return False
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        if self.last_code_result and self.last_code_result.get("code"):
            self._used_codes.add(self.last_code_result["code"])
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE.get(self.platform_key)
            if cache and str(cache.get("activation_id")) == str(activation_id):
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                self._save_cache(cache)
        if self._resend_callback:
            try:
                self._resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 1})
        except Exception:
            pass

    def mark_send_failed(
        self, activation_id: str, reason: str = "", *, record_failure: Optional[bool] = None
    ) -> None:
        # 业务侧拒了这个号 → cancel 退款（号根本没用上，不能让主人白花钱）
        cancel_ok = self.cancel(activation_id, record_failure=False)
        # 简化原因显示：只保留前 80 字符
        short_reason = (reason or "未知原因")[:80]
        logger.info(
            "%s 号 activation_id=%s cancel 退款 %s (原因: %s)",
            self.platform_key,
            activation_id,
            "已取消" if cancel_ok else "已进入后台重试",
            short_reason,
        )
        activation = self.current_activation
        if activation and _phone_rejected_reason(reason):
            if _add_phone_blacklist(activation.phone_number):
                logger.info("号码 %s 已加入持久化黑名单", activation.phone_number)
        # 只有明确的号码拒绝才计入号码失败；IP/账号频控、网络和服务端错误不能污染号段质量。
        should_record = _phone_rejected_reason(reason) if record_failure is None else bool(record_failure)
        if should_record:
            self._record_current_result(activation_id, success=False)
        self._release_current_reservation(activation_id)
        # 同时清掉复用缓存（避免下次注册又拿到这个被拒的号）
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE.get(self.platform_key)
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["reuse_stopped"] = True
                cache["stop_reason"] = reason or "phone rejected"
                self._save_cache(cache)
                self._clear_cache()

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._resend_callback = callback

    def set_should_stop(self, callback: Optional[Callable[[], bool]]) -> None:
        self._should_stop_callback = callback

    def _stop_requested(self) -> bool:
        callback = self._should_stop_callback
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _raise_if_stopped(self) -> None:
        if self._stop_requested():
            raise RuntimeError("SMS 接码因任务停止而中止")



# ---------------------------------------------------------------------------
# 工厂 + 回调控制器（注入到 auth_flow）
# ---------------------------------------------------------------------------


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """从配置创建 provider 实例。

    provider_key: smsbower / herosms
    config 字段：sms_api_key / sms_country / sms_service / sms_max_price /
                sms_reuse_phone / sms_phone_success_max
    """
    pk = _normalise_sms_provider_key(provider_key)
    legacy_provider = str(config.get("sms_provider") or "smsbower").strip().lower()
    api_key = str(config.get(f"sms_{pk}_api_key") or "").strip()
    if not api_key and legacy_provider == pk:
        api_key = str(config.get("sms_api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"{pk} 未配置 API Key")
    country = str(config.get("sms_country") or "").strip()
    service = str(config.get("sms_service") or "").strip() or "dr"
    # 接码平台默认直连，避免注册代理的抖动同时拖垮两个供应商；需要时可单独设 sms_proxy。
    proxy = str(config.get("sms_proxy") or "").strip() or None
    max_price = _safe_float(config.get("sms_max_price"), -1)
    reuse = _safe_bool(config.get("sms_reuse_phone"), False)
    succ_max = max(0, _safe_int(config.get("sms_phone_success_max"), 3))
    supplier_strategy = _normalise_supplier_strategy(
        config.get("sms_supplier_strategy") or SMS_DEFAULT_SUPPLIER_STRATEGY
    )

    if pk in ("smsbower", "sms_bower"):
        return SmsBowerProvider(api_key=api_key,
                                base_url=str(config.get("sms_smsbower_api_url") or ""),
                                platform_key="smsbower",
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max,
                                supplier_strategy=supplier_strategy)
    if pk in ("herosms", "hero_sms"):
        return SmsBowerProvider(api_key=api_key,
                                base_url=str(config.get("sms_herosms_api_url") or "")
                                or "https://hero-sms.com/stubs/handler_api.php",
                                platform_key="herosms",
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max,
                                supplier_strategy=supplier_strategy)
    raise RuntimeError(f"未知接码服务: {provider_key}")


def _has_explicit_sms_provider_flags(config: dict) -> bool:
    return any(key in config for key in ("sms_smsbower_enabled", "sms_herosms_enabled"))


def get_enabled_sms_providers(config: dict) -> list[SmsBowerProvider]:
    """按主平台优先顺序构造启用的平台；旧版单平台配置无需迁移。"""
    mode = _normalise_sms_mode(config.get("sms_mode"))
    legacy_provider = str(config.get("sms_provider") or "smsbower").strip().lower()
    if legacy_provider not in {"smsbower", "herosms"}:
        legacy_provider = "smsbower"
    platform_order = [legacy_provider] + [
        platform for platform in ("smsbower", "herosms") if platform != legacy_provider
    ]
    has_explicit_flags = _has_explicit_sms_provider_flags(config)
    result = []
    for platform in platform_order:
        key = str(config.get(f"sms_{platform}_api_key") or "").strip()
        if not key and legacy_provider == platform:
            key = str(config.get("sms_api_key") or "").strip()
        flag_key = f"sms_{platform}_enabled"
        if mode == "race":
            enabled = (
                _safe_bool(config.get(flag_key), False)
                if has_explicit_flags
                else platform == legacy_provider
            )
        elif mode in {"split", "session_race"}:
            # 新的非竞速分流模式：迁移配置即使没有 *_enabled 字段，也按已配置
            # API Key 自动纳入两个平台；显式 false 仍然可以停用某个平台。
            enabled = (
                _safe_bool(config.get(flag_key), bool(key))
                if has_explicit_flags
                else bool(key)
            )
        else:
            enabled = platform == legacy_provider and (
                _safe_bool(config.get(flag_key), False) if flag_key in config else True
            )
        if enabled and key:
            result.append(create_sms_provider(platform, config))
    if (
        not result
        and not has_explicit_flags
        and str(config.get("sms_api_key") or "").strip()
    ):
        result.append(create_sms_provider(legacy_provider, config))
    return result


class PhoneCallbackController:
    """把 SMS provider 包装成两阶段回调，注入到 auth_flow.add_phone 流程。

    用法（在 auth_flow._handle_add_phone_verification 里）：
        controller = PhoneCallbackController(...)
        phone = controller.get_phone()         # 阶段1：租号
        flow._add_phone_send(phone)
        ...
        code = controller.get_code()           # 阶段2：等 SMS 验证码
        flow._phone_otp_validate(code)
        controller.report_success()            # 成功
        # 失败时 controller.cancel() / mark_code_failed()
    """

    def __init__(
        self,
        provider_key: str,
        config: dict,
        *,
        service: str = "openai",
        country: str = "",
        log_fn: Optional[Callable[[str], None]] = None,
        auto_select_country: bool = False,
        config_provider: Optional[Callable[[], dict]] = None,
        forced_provider_key: str = "",
    ):
        forced = _normalise_sms_provider_key(forced_provider_key)
        if forced and forced not in {"smsbower", "herosms"}:
            raise RuntimeError(f"未知强制接码平台: {forced_provider_key}")
        self.forced_provider_key = forced
        self.provider_key = forced or _normalise_sms_provider_key(provider_key) or "smsbower"
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.auto_select_country = bool(auto_select_country)
        self.config_provider = config_provider
        self.should_stop: Optional[Callable[[], bool]] = None
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.completed = False
        self.peer_cancelled = False
        self.abort_reason = ""
        self._abort_event = threading.Event()
        self._state_lock = threading.RLock()
        self._acquired_reuse_locks: list[threading.RLock] = []
        self._pending_rt_selection: Optional[dict] = None
        self._rt_reported = False
        self.config = self._force_provider_config(self.config)

    def _force_provider_config(self, config: dict) -> dict:
        cfg = dict(config or {})
        if not self.forced_provider_key:
            return cfg
        selected = self.forced_provider_key
        other = "herosms" if selected == "smsbower" else "smsbower"
        original_provider = _normalise_sms_provider_key(cfg.get("sms_provider"))
        selected_key = str(cfg.get(f"sms_{selected}_api_key") or "").strip()
        if not selected_key and original_provider == selected:
            selected_key = str(cfg.get("sms_api_key") or "").strip()
        cfg["sms_mode"] = "single"
        cfg["sms_provider"] = selected
        cfg[f"sms_{selected}_enabled"] = True
        cfg[f"sms_{other}_enabled"] = False
        # 不允许把另一主平台的 legacy key 误用到强制平台。
        cfg["sms_api_key"] = selected_key
        cfg[f"sms_{selected}_api_key"] = selected_key
        return cfg

    def _stop_requested(self) -> bool:
        if self._abort_event.is_set():
            return True
        callback = self.should_stop
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _raise_if_stopped(self) -> None:
        if self._stop_requested():
            raise RuntimeError("SMS 接码因任务停止而中止")

    def _bind_provider_stop(self, provider: BaseSmsProvider) -> None:
        try:
            provider.set_should_stop(self._stop_requested)
        except Exception:
            pass

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self._raise_if_stopped()
            if self.forced_provider_key:
                self.config = self._force_provider_config(self.config)
                self.provider = create_sms_provider(self.forced_provider_key, self.config)
                self._bind_provider_stop(self.provider)
                return self.provider
            providers = get_enabled_sms_providers(self.config)
            if not providers:
                if _has_explicit_sms_provider_flags(self.config):
                    raise RuntimeError("没有启用且配置完整的接码平台")
                self.provider = create_sms_provider(self.provider_key, self.config)
            else:
                self.provider = providers[0]
            self._bind_provider_stop(self.provider)
        return self.provider

    def _country_candidates(self, provider: SmsBowerProvider) -> list[str]:
        # 收集候选国家列表：用户多选 > 自动选号选出的 best > 单一 country
        allowed_raw = str(self.config.get("sms_allowed_countries") or "").strip()
        allowed_list = [c.strip() for c in allowed_raw.replace(";", ",").split(",") if c.strip()]
        country_candidates: list[str] = []

        def _top_rows() -> list[dict]:
            # 单飞与缓存已下沉到 get_top_countries，这里不再加全局锁。
            return provider.get_top_countries(service=self.service)

        if self.auto_select_country:
            try:
                min_stock = _safe_int(self.config.get("sms_auto_min_stock"), 20)
                max_price = _safe_float(self.config.get("sms_auto_max_price"), 0)
                strict = _safe_bool(self.config.get("sms_strict_whitelist"), False)
                success_first = provider.supplier_strategy == "success_first"
                raw_rows = [dict(row) for row in _top_rows()]
                rows = _rank_country_rows(provider.platform_key, raw_rows)

                if provider.platform_key == "herosms":
                    # Hero must see the complete live catalog. The configured
                    # auto price is a soft threshold: under-threshold countries
                    # are exhausted first, then higher prices remain as fallback.
                    eligible = [
                        row for row in rows
                        if (not allowed_list or str(row.get("country") or "") in allowed_list)
                        and (not strict or str(row.get("country") or "") in OPENAI_SMS_COUNTRIES)
                    ]
                    eligible.sort(
                        key=lambda row: _hero_price_priority_key(
                            row, threshold=max_price, min_stock=min_stock
                        )
                    )
                    country_candidates = [str(row["country"]) for row in eligible]
                    if allowed_list:
                        country_candidates.extend(
                            cid for cid in allowed_list if cid not in country_candidates
                        )
                elif allowed_list:
                    in_allow = [
                        row for row in rows
                        if str(row.get("country") or "") in allowed_list
                    ]
                    country_candidates = [str(row["country"]) for row in in_allow]
                    country_candidates.extend(
                        cid for cid in allowed_list if cid not in country_candidates
                    )
                else:
                    if success_first:
                        # The catalog can omit countries that remain rentable.
                        # Reintroduce only historically proven countries; the
                        # live getNumber call remains the source of truth.
                        catalog_countries = {
                            str(row.get("country") or "").strip() for row in raw_rows
                        }
                        for historical_country, (rate, samples) in (
                            _country_quality_map(provider.platform_key).items()
                        ):
                            if (
                                historical_country not in catalog_countries
                                and samples >= _SMS_MIN_COUNTRY_SAMPLES
                                and rate >= _SMS_BAD_COUNTRY_RATE
                            ):
                                raw_rows.append({
                                    "country": historical_country,
                                    "price": max_price if max_price > 0 else provider.max_price,
                                    "count": 0,
                                    "score": float("inf"),
                                    "history_only": True,
                                })
                        rows = _rank_country_rows(provider.platform_key, raw_rows)
                    eligible = [
                        row for row in rows
                        if (
                            success_first
                            or max_price <= 0
                            or _safe_float(row.get("price"), 0) <= max_price
                        )
                        and (not strict or str(row.get("country")) in OPENAI_SMS_COUNTRIES)
                    ]
                    if success_first:
                        eligible = [
                            row for row in eligible
                            if int(row.get("quality_tier") or 0) < 2
                        ]
                        if not eligible:
                            raise SmsTemporarilyUnavailable(
                                "暂无达到最低历史成功率的接码国家",
                                _SMS_INVENTORY_COOLDOWN_SECONDS,
                            )
                    stocked = [
                        str(row["country"])
                        for row in eligible
                        if _safe_int(row.get("count"), 0) >= max(1, min_stock)
                    ]
                    fallback = [
                        str(row["country"])
                        for row in eligible
                        if str(row["country"]) not in stocked
                    ]
                    country_candidates = (
                        [str(row["country"]) for row in eligible]
                        if success_first
                        else stocked + fallback
                    )
            except SmsTemporarilyUnavailable:
                raise
            except Exception as e:
                logger.warning("%s 国家智能选择失败: %s", provider.platform_key, e)
                country_candidates = list(allowed_list) or ([self.country] if self.country else [])
        else:
            country_candidates = [self.country] if self.country else []
        if not country_candidates:
            country_candidates = [SMS_DEFAULT_COUNTRY]
        return country_candidates

    @staticmethod
    def _cancel_late_activation(future: Future, provider: SmsBowerProvider) -> None:
        if future.cancelled():
            return
        try:
            activation = future.result()
            provider.cancel(activation.activation_id, record_failure=False)
        except Exception:
            pass

    def _acquire_from(self, provider: SmsBowerProvider) -> SmsActivation:
        self._raise_if_stopped()
        self._bind_provider_stop(provider)
        candidates = self._country_candidates(provider)
        label_parts = []
        quality_by_country = _country_quality_map(provider.platform_key)
        for candidate_country in candidates[:5]:
            history_rate, history_samples = quality_by_country.get(candidate_country, (0.5, 0))
            quality_label = (
                f"{history_rate * 100:.1f}%/{history_samples}" if history_samples else "new"
            )
            label_parts.append(
                f"{candidate_country}({SMS_COUNTRY_NAMES_CN.get(candidate_country, '?')},"
                f"{quality_label})"
            )
        labels = ",".join(label_parts)
        self.log(
            f"准备租号: platform={provider.platform_key} service={self.service} "
            f"候选={labels}{' ...' if len(candidates) > 5 else ''}"
        )
        activation = provider.get_number(
            service=self.service,
            country=candidates[0],
            country_candidates=candidates,
        )
        if self._stop_requested():
            try:
                provider.cancel(activation.activation_id, record_failure=False)
            except Exception:
                pass
            raise RuntimeError("SMS 接码因任务停止而中止")
        return activation

    def _acquire_split(
        self,
        providers: list[SmsBowerProvider],
        *,
        selected: Optional[SmsBowerProvider] = None,
    ) -> tuple[SmsBowerProvider, SmsActivation]:
        """按 1:1 分流串行取号；首选失败后才尝试另一个平台。"""
        selected = selected or _select_split_provider(providers)
        ordered = [selected] + [
            provider for provider in _split_platform_order(providers) if provider is not selected
        ]
        errors: list[str] = []
        for index, provider in enumerate(ordered):
            self._raise_if_stopped()
            if index > 0:
                # 复用号码锁按平台隔离；回退前释放首选平台，避免双平台互相等待。
                self._release_lock()
                self._acquire_reuse_locks([provider])
                _record_split_attempt(providers, provider)
            try:
                activation = self._acquire_from(provider)
                self.log(
                    f"双平台分流取号: platform={provider.platform_key} "
                    f"顺序={index + 1}/{len(ordered)}"
                )
                return provider, activation
            except Exception as exc:
                errors.append(f"{provider.platform_key}: {exc}")
                # 平台封禁由 provider 层持久化冷却；本次只做串行兜底，绝不并行再租一个号。
                if index + 1 < len(ordered):
                    self.log(f"分流平台 {provider.platform_key} 取号失败，顺序切换到下一个平台")
        raise RuntimeError("双平台分流取号均失败: " + " | ".join(errors))

    def _acquire_reuse_locks(self, providers: list[BaseSmsProvider]) -> None:
        """只在启用号码复用时串行同平台；普通租号可跨 worker 并发。"""
        if self._acquired_reuse_locks:
            return
        platforms = sorted({
            str(getattr(provider, "platform_key", "smsbower") or "smsbower").lower()
            for provider in providers
            if bool(getattr(provider, "reuse_phone_to_max", False))
        })
        for platform in platforms:
            lock = _platform_reuse_lock(platform)
            lock.acquire()
            self._acquired_reuse_locks.append(lock)

    def get_phone(self) -> str:
        """阶段 1：单平台、双平台 1:1 分流，或兼容旧版双平台竞速。"""
        self._raise_if_stopped()
        if self.config_provider is not None:
            live_config = self.config_provider() or {}
            self.config = self._force_provider_config(live_config)
            if not self.config.get("sms_enabled"):
                raise RuntimeError("SMS 配置已关闭，停止本次取号")
        if self.forced_provider_key:
            providers = [self._provider()]
        else:
            providers = get_enabled_sms_providers(self.config)
        if not providers:
            if _has_explicit_sms_provider_flags(self.config):
                raise RuntimeError("没有启用且配置完整的接码平台")
            providers = [create_sms_provider(self.provider_key, self.config)]
        for candidate_provider in providers:
            self._bind_provider_stop(candidate_provider)
        healthy_providers = [
            provider for provider in providers
            if not _is_platform_banned(getattr(provider, "platform_key", ""))
        ]
        if healthy_providers:
            skipped = [p.platform_key for p in providers if p not in healthy_providers]
            if skipped:
                self.log(f"跳过冷却平台: {', '.join(skipped)}")
            providers = healthy_providers
        elif providers:
            retry_after = min(_platform_ban_remaining(p.platform_key) for p in providers)
            raise SmsTemporarilyUnavailable("所有接码平台都在封禁冷却中", retry_after)
        errors = []
        mode = _normalise_sms_mode(self.config.get("sms_mode"))
        if mode == "split" and len(providers) > 1:
            selected = _select_split_provider(providers)
            self._acquire_reuse_locks([selected])
            try:
                provider, activation = self._acquire_split(providers, selected=selected)
            except Exception:
                self._release_lock()
                raise
        elif len(providers) == 1:
            provider = providers[0]
            self._acquire_reuse_locks([provider])
            try:
                activation = self._acquire_from(provider)
            except Exception:
                self._release_lock()
                raise
        else:
            self._acquire_reuse_locks(providers)
            provider = None
            activation = None
        if mode != "split" and len(providers) > 1:
            strategy = _normalise_supplier_strategy(
                self.config.get("sms_supplier_strategy") or SMS_DEFAULT_SUPPLIER_STRATEGY
            )
            configured_decision_ms = max(
                0, _safe_int(self.config.get("sms_race_decision_ms"), 800)
            )
            decision_ms = 0 if strategy == "success_first" else configured_decision_ms
            self.log(
                f"双平台智能取号: {', '.join(p.platform_key for p in providers)} "
                f"策略={strategy} 对冲延迟={_safe_int(self.config.get('sms_race_hedge_ms'), 2000)}ms "
                f"决策窗口={decision_ms}ms"
            )
            executor = ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="sms-race")
            futures = {}
            pending = set()
            candidates: list[tuple[int, BaseSmsProvider, SmsActivation]] = []
            order = 0

            def _submit(provider_to_start: BaseSmsProvider) -> None:
                future = executor.submit(self._acquire_from, provider_to_start)
                futures[future] = provider_to_start
                pending.add(future)

            def _collect(done_futures) -> None:
                nonlocal order
                for future in done_futures:
                    candidate_provider = futures[future]
                    try:
                        candidates.append((order, candidate_provider, future.result()))
                        order += 1
                    except Exception as exc:
                        errors.append(f"{candidate_provider.platform_key}: {exc}")

            # 先给主平台一个短窗口；只有主平台慢或失败才启动对冲平台。
            # 这避免每个任务默认消耗两个号码，同时保留慢平台兜底。
            _submit(providers[0])
            hedge_ms = max(0, _safe_int(self.config.get("sms_race_hedge_ms"), 2000))
            done, still_pending = wait(pending, timeout=hedge_ms / 1000)
            pending = still_pending
            _collect(done)
            if not candidates:
                for provider_to_start in providers[1:]:
                    _submit(provider_to_start)
            while pending and not candidates:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                _collect(done)
            if candidates and pending and decision_ms > 0:
                done, pending = wait(pending, timeout=decision_ms / 1000)
                for future in done:
                    candidate_provider = futures[future]
                    try:
                        candidates.append((order, candidate_provider, future.result()))
                        order += 1
                    except Exception as exc:
                        errors.append(f"{candidate_provider.platform_key}: {exc}")

            def _quality(item: tuple[int, BaseSmsProvider, SmsActivation]) -> tuple[float, int]:
                arrival, candidate_provider, candidate_activation = item
                metadata = candidate_activation.metadata or {}
                score = metadata.get("selection_score")
                if score is None:
                    score = _candidate_score(
                        candidate_provider.platform_key,
                        candidate_activation.country,
                        int(metadata.get("provider_id") or 0),
                        float(metadata.get("price") or 0),
                        int(metadata.get("count") or 0),
                        strategy,
                    )
                return float(score), arrival

            provider = None
            activation = None
            if candidates:
                _, provider, activation = min(candidates, key=_quality)
                self.log(
                    f"双平台质量决策胜出: platform={provider.platform_key} "
                    f"score={_quality((0, provider, activation))[0]:.6f}"
                )
                for _, candidate_provider, candidate_activation in candidates:
                    if candidate_activation is activation:
                        continue
                    try:
                        candidate_provider.cancel(
                            candidate_activation.activation_id, record_failure=False
                        )
                    except Exception:
                        pass
            for future in pending:
                candidate_provider = futures[future]
                future.add_done_callback(
                    lambda f, p=candidate_provider: self._cancel_late_activation(f, p)
                )
            executor.shutdown(wait=False, cancel_futures=True)
            if activation is None or provider is None:
                self._release_lock()
                raise RuntimeError("双平台取号均失败: " + " | ".join(errors))
            if self._stop_requested():
                try:
                    provider.cancel(activation.activation_id, record_failure=False)
                except Exception:
                    pass
                self._release_lock()
                raise RuntimeError("SMS 接码因任务停止而中止")

        if self._stop_requested():
            try:
                provider.cancel(activation.activation_id, record_failure=False)
            except Exception:
                pass
            self._release_lock()
            raise RuntimeError("SMS 接码因任务停止而中止")
        if self.forced_provider_key and provider.platform_key != self.forced_provider_key:
            try:
                provider.cancel(activation.activation_id, record_failure=False)
            except Exception:
                pass
            self._release_lock()
            raise RuntimeError(
                f"强制平台越界: expected={self.forced_provider_key} actual={provider.platform_key}"
            )
        with self._state_lock:
            # abort 可能恰好发生在租号返回和状态交接之间，最后再做一次原子检查。
            if self._abort_event.is_set():
                cancelled_after_acquire = True
            else:
                cancelled_after_acquire = False
                self.provider = provider
                self.provider_key = self.forced_provider_key or provider.platform_key
                self.activation = activation
        if cancelled_after_acquire:
            try:
                provider.cancel(activation.activation_id, record_failure=False)
            except Exception:
                pass
            self._release_lock()
            raise RuntimeError("SMS 接码因任务停止而中止")

        reused = bool((self.activation.metadata or {}).get("reused"))
        used_country = self.activation.country or self.country or SMS_DEFAULT_COUNTRY
        used_country_label = f"{used_country} {SMS_COUNTRY_NAMES_CN.get(used_country, '')}"
        self.log(
            f"取号胜出: platform={provider.platform_key} 号码={self.activation.phone_number}"
            f"{'(复用)' if reused else ''} 国家={used_country_label}"
        )
        return self.activation.phone_number

    def get_code(self, timeout: int = 180) -> str:
        """阶段 2：等待 SMS 验证码。"""
        self._raise_if_stopped()
        with self._state_lock:
            activation = self.activation
            provider = self.provider
        if not activation:
            raise RuntimeError("PhoneCallbackController: 未先 get_phone")
        provider = provider or self._provider()
        self._bind_provider_stop(provider)
        self.log(f"⏳ 等待 SMS 验证码... (activation_id={activation.activation_id} timeout={timeout}s)")
        code = provider.get_code(activation.activation_id, timeout=timeout)
        self._raise_if_stopped()
        if code:
            self.log(f"✅ 收到 SMS 验证码: {code}")
            if getattr(provider, "auto_report_success_on_code", True):
                self.report_success()
        else:
            self.log(f"⚠️ 未收到 SMS 验证码: activation_id={activation.activation_id}")
        return code

    def report_success(self) -> None:
        if self._stop_requested():
            self._release_lock()
            return
        with self._state_lock:
            activation = self.activation
            provider = self.provider
        if activation and provider and not self.completed:
            metadata = activation.metadata or {}
            self._pending_rt_selection = {
                "platform": provider.platform_key,
                "country": activation.country,
                "provider_id": int(metadata.get("provider_id") or 0),
            }
            try:
                provider.report_success(activation.activation_id)
            except Exception as e:
                logger.warning("report_success 失败: %s", e)
            self.completed = True
            self.log(f"🎉 已标记号码成功完成: activation_id={activation.activation_id}")
        self._release_lock()

    def mark_code_failed(self, reason: str = "") -> None:
        if self._stop_requested():
            return
        if self.activation and self.provider:
            try:
                self.provider.mark_code_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def mark_send_succeeded(self) -> None:
        if self._stop_requested():
            return
        if self.activation and self.provider:
            try:
                self.provider.mark_send_succeeded(self.activation.activation_id)
            except Exception:
                pass

    def mark_send_failed(self, reason: str = "", *, record_failure: Optional[bool] = None) -> None:
        if self.activation and self.provider:
            try:
                kwargs = {"reason": reason}
                if record_failure is not None:
                    kwargs["record_failure"] = record_failure
                self.provider.mark_send_failed(self.activation.activation_id, **kwargs)
            except Exception:
                pass
        self.activation = None
        self.provider = None

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        if self._stop_requested():
            return
        try:
            self._provider().set_resend_callback(callback)
        except Exception:
            pass

    def cleanup(self) -> None:
        """流程结束（成功或失败）调用：释放未完成的号、解锁。"""
        if self.activation and not self.completed and self.provider:
            try:
                self.provider.cancel(self.activation.activation_id)
                self.log(f"🗑️ 已释放未使用号码: activation_id={self.activation.activation_id}")
            except Exception:
                pass
        self.activation = None
        if not self.completed:
            self.provider = None
        self._release_lock()

    def abort(self, reason: str = "peer_won") -> bool:
        """幂等中止当前会话；未完成号码按中性样本取消，不污染供应商统计。"""
        reason_text = str(reason or "peer_won").strip() or "peer_won"
        with self._state_lock:
            if self._abort_event.is_set():
                return False
            self.abort_reason = reason_text
            normalized_reason = reason_text.lower().replace("-", "_").replace(" ", "_")
            self.peer_cancelled = normalized_reason.startswith("peer_won")
            self._abort_event.set()
            activation = self.activation
            provider = self.provider
            completed = self.completed
            self.activation = None
            self.provider = None

        if provider is not None:
            try:
                provider.set_resend_callback(None)
            except Exception:
                pass
        if activation and not completed and provider:
            try:
                provider.cancel(activation.activation_id, record_failure=False)
                self.log(
                    f"接码会话已中止({reason_text})，释放未使用号码: "
                    f"activation_id={activation.activation_id}"
                )
            except Exception:
                pass
        self._release_lock()
        return True

    def abort_peer_won(self) -> None:
        """兼容旧调用；新代码使用 abort(reason='peer_won')。"""
        self.abort("peer_won")

    def report_rt_result(self, success: bool) -> None:
        """把最终是否拿到 refresh_token 回灌到供应商质量统计。"""
        if self.peer_cancelled:
            self._rt_reported = True
            return
        if self._rt_reported or not self._pending_rt_selection:
            return
        selected = self._pending_rt_selection
        _record_stats(
            str(selected.get("platform") or self.provider_key),
            str(selected.get("country") or ""),
            int(selected.get("provider_id") or 0),
            success=bool(success),
            stage="rt",
        )
        self._rt_reported = True
        self.log(
            f"RT 结果已回灌供应商评分: platform={selected.get('platform')} "
            f"provider={selected.get('provider_id')} success={bool(success)}"
        )

    def _release_lock(self) -> None:
        while self._acquired_reuse_locks:
            lock = self._acquired_reuse_locks.pop()
            try:
                lock.release()
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# 简单 CLI 测试
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python sms_provider.py <provider_key> <api_key> [country]")
        sys.exit(1)
    pk = sys.argv[1]
    key = sys.argv[2]
    cc = sys.argv[3] if len(sys.argv) > 3 else ""
    p = create_sms_provider(pk, {"sms_api_key": key, "sms_country": cc})
    print(f"余额: {p.get_balance()}")
