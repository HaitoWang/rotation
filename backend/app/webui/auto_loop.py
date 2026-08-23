"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个 worker 按 worker index 取一个代理（round-robin），避免同 IP 多号
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections import Counter, deque
from typing import Optional
from urllib.parse import urlsplit

from . import db, registrar
from ..mail_providers import MailProviderError, get_provider_class

logger = logging.getLogger("auto_loop")


class _RunStopToken:
    """Per-run cancellation combined with the controller-wide stop event."""

    def __init__(self, global_event: threading.Event):
        self._global_event = global_event
        self._local_event = threading.Event()

    def is_set(self) -> bool:
        return self._global_event.is_set() or self._local_event.is_set()

    def cancel(self) -> None:
        self._local_event.set()


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


_PROXY_SESSION_RE = re.compile(
    r"(?:^|[-_])session[-_=]?([a-z0-9]+)(?=[-_:]|$)", re.IGNORECASE
)


def _proxy_session_key(proxy: str) -> str:
    """Return a stable route identity, preferring the proxy session id."""
    value = (proxy or "").strip()
    if not value:
        return "direct"
    parsed = urlsplit(value if "://" in value else "//" + value)
    match = _PROXY_SESSION_RE.search(parsed.username or value)
    if match:
        return f"session:{match.group(1).lower()}"
    return "proxy:" + value


def _parse_proxy_pool(text: str) -> list[str]:
    """Parse and deduplicate proxies by session identity, preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        key = _proxy_session_key(s)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _phase_from_event(kind: str, payload: dict, current: str) -> str:
    if kind == "phase":
        phase = str(payload.get("phase") or "").strip().lower()
        return {
            "starting": "启动注册",
            "mailbox_created": "邮箱准备",
            "session_race": "会话竞速",
            "binding_2fa": "绑定 2FA",
            "2fa_bound": "绑定 2FA",
            "export": "导出",
            "export_done": "导出",
            "sms": "短信验证",
            "sms_queue": "接码排队",
        }.get(phase, current or "注册流程")
    if kind != "log":
        return current
    message = str(payload.get("message") or "").lower()
    checks = (
        ("接码排队", ("接码资源暂不可用", "暂时无可用容量", "所有接码平台都在封禁冷却")),
        ("短信验证", ("[sms]", "add-phone", "phone-otp", "等待 sms")),
        ("Codex OAuth", ("codex oauth", "oauth_codex", "refresh_token")),
        ("邮箱 OTP", ("graph api 取 otp", "imap 取 otp", "等待邮箱", "提交邮箱验证码")),
        ("绑定 2FA", ("2fa", "totp")),
        ("注册流程", ("warmup", "提交注册邮箱", "注册密码", "[register] 开始")),
    )
    for phase, markers in checks:
        if any(marker in message for marker in markers):
            return phase
    return current


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（正整数，不设固定上限）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._concurrency: int = 1
        self._proxy_owner: dict[str, int] = {}
        self._worker_proxy: dict[int, str] = {}
        self._success_times: deque[float] = deque()
        self._stuck_timeout_seconds = 30 * 60
        self._stuck_cancel_grace_seconds = 90
        # 目标成功数：0 = 不限量（保持旧行为）；>0 时累计成功达标即自动停止
        self._target_count: int = 0

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            # 解析并发参数
            try:
                requested_concurrency = int((options or {}).get("concurrency") or 1)
            except (TypeError, ValueError):
                requested_concurrency = 1
            requested_concurrency = max(1, requested_concurrency)
            pool = _parse_proxy_pool((options or {}).get("proxy_pool") or "")
            single_proxy = str((options or {}).get("proxy") or "").strip()
            route_count = len(pool) if pool else (1 if single_proxy else 1)
            if route_count < requested_concurrency:
                return {
                    "ok": False,
                    "error": (
                        f"独立代理 session 不足：并发要求 {requested_concurrency}，"
                        f"去重后仅 {route_count} 个"
                    ),
                }
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._worker_status.clear()
            self._proxy_owner.clear()
            self._worker_proxy.clear()
            self._success_times.clear()
            self._last_message = "auto-loop 启动"
            self._concurrency = requested_concurrency
            self._proxy_pool = pool
            # 目标成功数（0=不限量）
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            self._stuck_timeout_seconds = max(
                60, int(self._options.get("stuck_timeout_seconds") or 30 * 60)
            )
            self._stuck_cancel_grace_seconds = max(
                10, int(self._options.get("stuck_cancel_grace_seconds") or 90)
            )
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "已请求停止（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                    "phase": info.get("phase", "启动注册"),
                    "age_seconds": round(max(0.0, now - float(info.get("started_at") or now)), 1),
                    "idle_seconds": round(max(
                        0.0,
                        now - float(info.get("last_activity_at") or info.get("started_at") or now),
                    ), 1),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            ages = sorted(item["age_seconds"] for item in workers_info)
            p95_index = max(0, min(len(ages) - 1, (95 * len(ages) + 99) // 100 - 1)) if ages else 0
            p95_age = ages[p95_index] if ages else 0
            stuck_threshold = self._stuck_timeout_seconds
            stuck_count = sum(
                1 for item in workers_info if item["idle_seconds"] >= stuck_threshold
            )
            phases = Counter(item["phase"] for item in workers_info)
            proxy_sessions = {
                _proxy_session_key(item["proxy"])
                for item in workers_info if item["proxy"]
            }
            window_seconds = min(300.0, max(1.0, now - self._started_at)) if self._started_at else 300.0
            while self._success_times and self._success_times[0] < now - 300:
                self._success_times.popleft()
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "effective_concurrency": max(0, len(workers_info) - stuck_count),
                "stage_counts": dict(sorted(phases.items())),
                "p95_task_age_seconds": p95_age,
                "stuck_task_count": stuck_count,
                "stuck_threshold_seconds": stuck_threshold,
                "independent_proxy_count": len(proxy_sessions),
                "success_per_minute": round(len(self._success_times) * 60.0 / window_seconds, 2),
                "success_rate_window_seconds": round(window_seconds),
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _proxy_for_worker(self, worker_id: int, retry_offset: int = 0) -> str:
        """Lease one session exclusively; retries rotate only to an unused session."""
        with self._lock:
            if not self._proxy_pool:
                return self._options.get("proxy", "") or ""
            old_proxy = self._worker_proxy.pop(worker_id, "")
            if old_proxy:
                self._proxy_owner.pop(_proxy_session_key(old_proxy), None)
            start = (worker_id + retry_offset) % len(self._proxy_pool)
            for offset in range(len(self._proxy_pool)):
                proxy = self._proxy_pool[(start + offset) % len(self._proxy_pool)]
                key = _proxy_session_key(proxy)
                if key not in self._proxy_owner:
                    self._proxy_owner[key] = worker_id
                    self._worker_proxy[worker_id] = proxy
                    return proxy
            if old_proxy:
                key = _proxy_session_key(old_proxy)
                self._proxy_owner[key] = worker_id
                self._worker_proxy[worker_id] = old_proxy
                return old_proxy
            raise RuntimeError("没有可分配的独立代理 session")

    def _release_worker_proxy(self, worker_id: int) -> None:
        """Release the session lease held by a worker that is exiting."""
        with self._lock:
            proxy = self._worker_proxy.pop(worker_id, "")
            if not proxy:
                return
            key = _proxy_session_key(proxy)
            if self._proxy_owner.get(key) == worker_id:
                self._proxy_owner.pop(key, None)

    def _observe_run(self, worker_id: int, run_id: str, kind: str, payload: dict) -> bool:
        with self._lock:
            info = self._worker_status.get(worker_id)
            if not info or info.get("run_id") != run_id:
                return False
            previous = info.get("phase", "启动注册")
            current = _phase_from_event(kind, payload, previous)
            info["phase"] = current
            info["last_activity_at"] = time.time()
            return current != previous

    def _record_finish(self, ok: bool, category: str):
        """worker 结束一个最终 run 后更新成功/失败计数。"""
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._success_times.append(time.time())
            else:
                self._registered_fail += 1
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            # 目标数量：累计成功达标 → 触发停止（stop_event 幂等，多 worker 同时命中也安全）
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，自动停止"
                    f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            logger.info(f"已达目标 {self._target_count} 个成功，触发自动停止")
            self._broadcast("state", self._snapshot())
            return
        self._broadcast("state", self._snapshot())

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_entry, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 轻微错峰即可；100 worker 约 5 秒启动完，不再固定等待 100 秒。
                time.sleep(0.05)
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._last_message = (
                    f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            self._broadcast("state", self._snapshot())

    def _worker_entry(self, worker_id: int):
        try:
            self._worker_loop(worker_id)
        finally:
            self._release_worker_proxy(worker_id)
            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        proxy_retry_offset = 0
        retry_email = ""
        proxy = self._proxy_for_worker(worker_id, proxy_retry_offset)
        logger.info(f"[worker-{worker_id}] 启动 (proxy={proxy or '直连'})")

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # 目标数量闸门：已成功 + 在跑的（复用 _worker_status 当在跑数）≥ 目标 → 本 worker 退出
            # 不新增易泄漏的计数器；_worker_status 已在锁内正常维护，最大限度压低超额
            with self._lock:
                if self._target_count and (
                    self._registered_ok + len(self._worker_status) >= self._target_count
                ):
                    logger.info(
                        f"[worker-{worker_id}] 目标 {self._target_count} 已锁定，退出"
                    )
                    return

            # claim 下一个号。要不要走号池由 provider 的 pooled 决定，
            # 非池化的（CF 这类自己造地址的）用虚拟占位。
            mail_source = db.get_setting("mail_source", "outlook")
            if retry_email:
                retry_account = db.get_account(retry_email)
                if not retry_account or str(retry_account.get("kind") or "").lower() != mail_source:
                    logger.warning(
                        f"[worker-{worker_id}] 邮箱来源已切换，放弃跨来源重试 {retry_email}"
                    )
                    retry_email = ""
            try:
                pooled = get_provider_class(mail_source).pooled
            except MailProviderError as e:
                logger.error(f"[worker-{worker_id}] {e}，停止")
                self._set_message(str(e))
                return
            if pooled:
                account = db.claim_account(retry_email) if retry_email else None
                if account is None:
                    account = db.claim_next(kind=mail_source)
            else:
                account = {
                    "email": f"{mail_source}_placeholder_"
                             f"{int(time.time())}_{worker_id}@placeholder.local",
                    "password": "", "client_id": "", "refresh_token": "",
                    "relay_url": "", "kind": mail_source,
                }
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                # 空 10 轮（约 30s）就停掉这个 worker
                if idle_round >= 10:
                    logger.info(f"[worker-{worker_id}] 号池空 30s，停止")
                    return
                # 等 3s 再试
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0
            retry_email = ""

            # 给这个 run 注入 worker 自己的代理
            run_options = dict(self._options)
            run_stop = _RunStopToken(self._stop_event)
            run_options["stop_event"] = run_stop
            if proxy:
                run_options["proxy"] = proxy

            # 启一个 run
            try:
                phase_box = {"phase": "启动注册"}

                def observe(observed_run_id, kind, payload, wid=worker_id, box=phase_box):
                    with self._lock:
                        previous = box["phase"]
                        box["phase"] = _phase_from_event(kind, payload, previous)
                    changed = box["phase"] != previous
                    changed = self._observe_run(wid, observed_run_id, kind, payload) or changed
                    if changed:
                        self._broadcast("state", self._snapshot())

                run_id = registrar.start_registration(account, run_options, observer=observe)
            except Exception as e:
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if pooled:
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "started_at": time.time(),
                    "last_activity_at": time.time(),
                    "phase": phase_box["phase"],
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": proxy,
            })

            # 等当前 run 跑完
            try:
                ok, category = self._wait_run_finish(
                    run_id,
                    timeout=self._stuck_timeout_seconds,
                    stop_event=run_stop,
                    worker_id=worker_id,
                )
                if category == "stuck":
                    message = (
                        f"worker-{worker_id} 检测到卡死任务 {run_id}，"
                        f"已取消并自动补位 {account['email']}"
                    )
                    logger.warning(message)
                    self._set_message(message)
                    if pooled:
                        db.defer_unused(account["email"], defer_seconds=120)
                    proxy_retry_offset += 1
                    proxy = self._proxy_for_worker(worker_id, proxy_retry_offset)
                    retry_email = ""
            finally:
                registrar.remove_run_observer(run_id)

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
            })

            if not ok and category == "stuck":
                # watchdog 已释放/延后邮箱；不计最终失败，由同一 worker 自动补位。
                pass
            elif not ok and category == "network":
                retry_email = account["email"] if pooled else ""
                old_proxy = proxy
                proxy_retry_offset += 1
                proxy = self._proxy_for_worker(worker_id, proxy_retry_offset)
                if proxy != old_proxy:
                    message = (
                        f"worker-{worker_id} 网络/环境错误，切换下一个代理后重试 "
                        f"{account['email']}"
                    )
                else:
                    message = (
                        f"worker-{worker_id} 网络/环境错误，当前无其他代理，"
                        f"重建会话后继续重试 {account['email']}"
                    )
                logger.warning(message)
                self._set_message(message)
            elif not ok and category == "rate_limit":
                message = (
                    f"worker-{worker_id} OpenAI 限流，邮箱已延后重试，继续下一个 "
                    f"{account['email']}"
                )
                logger.warning(message)
                self._set_message(message)
                self._record_finish(False, category)
            elif not ok and category == "sms_exhausted":
                message = (
                    f"worker-{worker_id} 达到最多换号次数，邮箱已延后重试，继续下一个 "
                    f"{account['email']}"
                )
                logger.warning(message)
                self._set_message(message)
                self._record_finish(False, category)
            else:
                self._record_finish(ok, category)

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _wait_run_finish(
        self,
        run_id: str,
        timeout: int = 0,
        stop_event=None,
        worker_id: Optional[int] = None,
    ) -> tuple[bool, str]:
        """Wait on the registrar event; use DB polling only after a restart."""
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None

        # Normal auto-loop runs live in this process and signal completion from
        # registrar._do_register. This removes one SQLite connection/query per
        # second for every worker while keeping the old fallback for runs that
        # predate a process restart.
        event_result = None
        while True:
            wait_timeout = None if deadline is None else min(5.0, max(0.0, deadline - time.monotonic()))
            event_result = registrar.wait_run_done(
                run_id, stop_event=stop_event or self._stop_event, timeout=wait_timeout,
            )
            if event_result is None or event_result:
                break
            if worker_id is not None:
                with self._lock:
                    info = self._worker_status.get(worker_id) or {}
                    if info.get("run_id") != run_id:
                        return False, "stuck"
                    last_activity = float(
                        info.get("last_activity_at") or info.get("started_at") or time.time()
                    )
                if time.time() - last_activity < timeout:
                    deadline = time.monotonic() + timeout
                    continue
            if not event_result:
                logger.warning("run %s 超过 %ss，触发卡死任务恢复", run_id, timeout)
                if hasattr(stop_event, "cancel"):
                    stop_event.cancel()
                recovered = registrar.wait_run_done(
                    run_id,
                    stop_event=stop_event or self._stop_event,
                    timeout=self._stuck_cancel_grace_seconds,
                )
                if not recovered:
                    logger.error(
                        "run %s 取消后 %ss 仍未退出；释放当前 worker 并切换代理 session",
                        run_id,
                        self._stuck_cancel_grace_seconds,
                    )
                return False, "stuck"
        if event_result is not None:
            con = db._conn()
            try:
                row = con.execute(
                    "SELECT status, error_category FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
            finally:
                con.close()
            if row:
                st = row["status"]
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row["error_category"] or "")

        # Fallback for a run restored after the app was restarted. This path is
        # intentionally unchanged semantically, but closes each short-lived
        # connection instead of leaking one per poll.
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        while deadline is None or time.monotonic() < deadline:
            con = db._conn()
            try:
                row = con.execute(
                    "SELECT status, error_category FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
            finally:
                con.close()
            if row:
                st = row["status"]
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row["error_category"] or "")
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        if timeout and timeout > 0:
            logger.warning("run %s 无进程内完成事件，按卡死任务回收邮箱并补位", run_id)
            try:
                con = db._conn()
                try:
                    row = con.execute(
                        "SELECT email, status FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                finally:
                    con.close()
                if row and row["email"]:
                    db.defer_unused(row["email"], defer_seconds=120)
            except Exception:
                logger.debug("回收无事件卡死任务邮箱失败", exc_info=True)
            return False, "stuck"
        return False, ""


# 全局单例
CONTROLLER = AutoLoopController()
