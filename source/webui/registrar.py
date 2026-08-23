"""注册 worker：调 auth_flow.run_register，并把日志/状态实时推到队列。

每个注册任务跑在独立线程；通过 `RunLogger` 把 `logging` 记录 + tail 状态推
到队列，前端用 SSE 实时收日志。
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]  # gpt-outlook-register/
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from auth_flow import AuthFlow  # noqa: E402
from mail_providers import (  # noqa: E402
    MailProviderError,
    create_mail_provider,
    get_provider_class,
)
from sms_provider import PhoneCallbackController, get_enabled_sms_providers  # noqa: E402

from . import db  # noqa: E402

# run_id -> queue of log strings; sentinel = None 表示流结束
_run_queues: dict[str, queue.Queue] = {}
# run_id -> completion event. auto_loop waits on this instead of polling SQLite.
_run_done_events: dict[str, threading.Event] = {}
# run_id -> its single log sink. Only one router is attached to root logger.
_run_sinks: dict[str, "QueueLogHandler"] = {}
# run_id -> lightweight in-process observer used by auto-loop metrics.
_run_observers: dict[str, object] = {}
_lock = threading.RLock()

# 当前线程正在跑哪个 run。
# ⚠️ 为什么需要这个：QueueLogHandler 是挂在 **root logger** 上的，而 root logger
#    是进程全局的。auto_loop 并发时 N 个 run 各挂一个 handler，每条日志会被
#    广播进**所有** run 的文件和 SSE 流 —— 实测 2026-08-04 三 worker 并发，
#    一个号的记录同时出现在 3 个 .log 里，WebUI 上三个号的日志搅在一起，
#    而 "[4/10] 获取 Sentinel Token..." 这类行不带邮箱，根本分不清是谁的。
#
#    注册链路（auth_flow / mail_providers / sentinel）内部不开任何线程，
#    一个 run 的日志全在自己那条线程上产生，所以线程绑定就能干净切开。
_current_run = threading.local()

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _queue_put(q: queue.Queue, item) -> None:
    """Never let a slow SSE consumer block a registration worker."""
    for _ in range(3):
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            # Preserve the newest state/log line. The file sink remains
            # complete. Retry if a concurrent consumer wins the race.
            try:
                q.get_nowait()
            except queue.Empty:
                continue


def _notify_run_observer(run_id: str, kind: str, payload: dict) -> None:
    with _lock:
        observer = _run_observers.get(run_id)
    if observer is None:
        return
    try:
        observer(kind, dict(payload))
    except Exception:
        logging.getLogger("registrar").debug("run observer failed", exc_info=True)


def register_run_observer(run_id: str, observer) -> None:
    with _lock:
        _run_observers[run_id] = observer


def remove_run_observer(run_id: str) -> None:
    with _lock:
        _run_observers.pop(run_id, None)


class QueueLogHandler(logging.Handler):
    """A per-run sink; it is fed by the one global root-log router."""

    def __init__(self, run_id: str, log_file: Path):
        super().__init__()
        self.run_id = run_id
        self._fh = open(log_file, "a", encoding="utf-8")
        self._write_lock = threading.Lock()
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord):
        # Keep direct/manual attachment safe; the global router uses
        # emit_record() after it has already selected this sink.
        if getattr(_current_run, "run_id", None) != self.run_id:
            return
        self.emit_record(record)

    def emit_record(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            with self._write_lock:
                if self._fh.closed:
                    return
                self._fh.write(msg + "\n")
                self._fh.flush()
            q = _run_queues.get(self.run_id)
            if q is not None:
                _queue_put(q, msg)
            _notify_run_observer(self.run_id, "log", {"message": msg})
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
        super().close()


class _RunLogRouter(logging.Handler):
    """Route a record directly to the sink for its current run thread."""

    def emit(self, record: logging.LogRecord):
        try:
            run_id = getattr(_current_run, "run_id", None)
            if not run_id:
                return
            with _lock:
                sink = _run_sinks.get(run_id)
            if sink is not None:
                sink.emit_record(record)
        except Exception:
            pass


_run_log_router = _RunLogRouter()
_run_log_router.setLevel(logging.INFO)
_root_logger = logging.getLogger()
if not any(isinstance(item, _RunLogRouter) for item in _root_logger.handlers):
    _root_logger.addHandler(_run_log_router)


_mailbox_group_locks_lock = threading.Lock()
_mailbox_group_locks: dict[str, threading.Lock] = {}


def _mailbox_group_lock(account: dict) -> Optional[threading.Lock]:
    """同一取码链接的 Gmail 主号/plus aliases 必须串行，避免消费错 OTP。"""
    if str(account.get("kind") or "").strip().lower() != "gmail_link":
        return None
    group_key = str(account.get("relay_url") or "").strip()
    if not group_key:
        return None
    with _mailbox_group_locks_lock:
        return _mailbox_group_locks.setdefault(group_key, threading.Lock())


def _acquire_group_lock(lock: Optional[threading.Lock], stop_event=None) -> None:
    if lock is None:
        return
    while True:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("任务停止：等待 Gmail 分裂邮箱独占锁时中止")
        if lock.acquire(timeout=0.25):
            return


def _emit_status(run_id: str, kind: str, payload: dict | str = ""):
    """前端约定：以 `__EVENT__:` 开头的行被解析成 JSON 状态事件。"""
    import json as _json
    q = _run_queues.get(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    _queue_put(q, "__EVENT__:" + _json.dumps(body, ensure_ascii=False))
    _notify_run_observer(run_id, kind, body)


# 网络/环境层错误特征：命中任一就把号放回 available（号本身没问题，是环境炸了）
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token 获取失败", "csrf token 失败",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy 失败", "网络预检查",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
    "invalid_state",
    "任务停止", "竞速对端完成",
]

# 服务端限流不等于代理断网，也不应马上拿同一个邮箱切代理死循环。
# registrar 会把邮箱延后释放，auto-loop 继续处理其他邮箱。
_RATE_LIMIT_ERROR_PATTERNS = [
    "rate_limit_exceeded", "too many requests", "http 429", "status=429",
    "status 429", "response 429", "限流 429",
]


def classify_error(err: str, mail_source: str = "") -> str:
    """分类错误：network / rate_limit / sms_exhausted / account / unknown。

    mail_source 用来问 provider 要不要豁免某些模式 —— 比如 iCloud 中转号
    本来就是买的老号，"已有账号"是正常流程不是失败（见
    MailProvider.accepts_existing_account）。留空则按最严格的规则判。
    """
    s = (err or "").lower()

    account_patterns = [
        "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
        "outlook imap account unusable", "user is authenticated but not connected",
        "outlook refresh failed", "authentication failed", "authenticate failed",
        "outlook otp timeout", "registration_disallowed",
        "mfa_challenge_missing_totp_secret",
        "totp_activated_but_persistence_failed",
        "existing_account_missing_password",
        "invalid_username_or_password", "invalid username or password",
        "deleted or deactivated", "account because it has been deleted",
        "account has been deactivated", "account is deactivated",
        "已有账号", "账号被", "refresh_token 失效",
    ]
    if mail_source:
        try:
            exempt = get_provider_class(mail_source).accepts_existing_account
        except MailProviderError:
            exempt = False  # 未知来源 —— 按默认最严格规则走
        # ⚠️ 用 if-in 而不是裸 remove()：上面的模式表将来被人改动/重排后，
        #    remove 抛的 ValueError 会跟 get_provider_class 的错混在同一个
        #    except 里被一起吞掉，豁免静默失效且没人看得出来。
        if exempt and "已有账号" in account_patterns:
            account_patterns.remove("已有账号")

    # 先匹配 account 特征（更具体），避免子串误命中（如 "outlook OTP timeout" 含 "timeout"）
    if any(p in s for p in account_patterns):
        return "account"
    if "sms 接码达到最多手机号尝试次数" in s:
        return "sms_exhausted"
    if any(p in s for p in _RATE_LIMIT_ERROR_PATTERNS):
        return "rate_limit"
    if any(p in s for p in _NETWORK_ERROR_PATTERNS):
        return "network"
    return "unknown"


def _session_env_overrides(
    options: dict,
    *,
    should_stop=None,
    register_password: str = "",
) -> dict:
    env_overrides = {
        "WEBUI_ALLOW_LOGIN": "1",
        "OTP_TIMEOUT": str(int(options.get("otp_timeout") or 180)),
    }
    if options.get("want_refresh_token", True):
        env_overrides["REQUIRE_REFRESH_TOKEN"] = "1"
    if should_stop is not None:
        env_overrides["_should_stop"] = should_stop
    if register_password:
        env_overrides["REGISTER_PASSWORD"] = register_password
    if not options.get("want_refresh_token", True):
        env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
        env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
        env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
    return env_overrides


def _run_flow_to_dict(flow: AuthFlow, mail, options: dict) -> tuple[dict, bool]:
    """执行一个无落库的 AuthFlow，并验证本次要求的凭证是否齐全。"""
    partial = False
    try:
        result = flow.run_register(mail)
        return result.to_dict(), partial
    except RuntimeError as exc:
        d = flow.result.to_dict()
        need_access = options.get("want_access_token", True)
        need_session = options.get("want_session_token", True)
        need_refresh = options.get("want_refresh_token", True)
        if need_refresh and not d.get("refresh_token"):
            raise RuntimeError(
                "Codex OAuth 未获取 refresh_token（add-phone 未完成或交换失败），"
                f"本次不计成功: {exc}"
            ) from exc
        wanted_ok = (
            (not need_access or d.get("access_token"))
            and (not need_session or d.get("session_token"))
            and (not need_refresh or d.get("refresh_token"))
        )
        has_any = bool(d.get("access_token") or d.get("refresh_token") or d.get("session_token"))
        if wanted_ok and has_any:
            logging.getLogger("registrar").warning(
                f"[register] 流程末段异常但用户勾选的凭证已齐: {exc}"
            )
        elif has_any:
            partial = True
            logging.getLogger("registrar").warning(
                f"[register] 部分凭证 (缺用户勾选的某项): {exc}"
            )
        else:
            raise
        return d, partial
    finally:
        release_email_lock = getattr(flow, "_release_email_phase_lock", None)
        if callable(release_email_lock):
            release_email_lock()


def _account_callback_for_flow(email: str) -> dict:
    try:
        data = db.get_registered(email)
        if data:
            return {
                "password": data.get("password", ""),
                "totp_secret": data.get("totp_secret", ""),
            }
    except Exception as exc:
        logging.getLogger("registrar").warning(f"[register] account_callback 异常: {exc}")
    return {}


def _close_flow(flow) -> None:
    try:
        session = getattr(flow, "session", None)
        if session is not None:
            session.close()
    except Exception:
        pass


def _run_session_race_auth(
    run_id: str,
    account: dict,
    options: dict,
    mail_source: str,
) -> dict:
    """并行执行 Hero/SmsBower 两个完整且无落库的注册会话。"""
    if str(mail_source or "").strip().lower() in {"gmail", "gmail_link"}:
        raise RuntimeError("Gmail 共用收件链路不支持双完整会话竞速")
    sms_cfg = db.get_sms_internal_config()
    provider_keys = {
        str(getattr(provider, "platform_key", "") or "").strip().lower()
        for provider in get_enabled_sms_providers(sms_cfg)
    }
    required = ("herosms", "smsbower")
    if not all(key in provider_keys for key in required):
        missing = ", ".join(key for key in required if key not in provider_keys)
        raise RuntimeError(f"双会话竞速缺少可用接码平台: {missing}")

    saved = db.get_registered(account.get("email", "")) or {}
    shared_password = (saved.get("password") or "").strip() or AuthFlow._random_password()
    email_phase_lock = threading.Lock()
    global_stop = options.get("stop_event")
    cancel_events = {key: threading.Event() for key in required}
    done_events = {key: threading.Event() for key in required}
    state_lock = threading.Lock()
    ready = threading.Event()
    state = {"winner": None, "errors": {}, "flows": {}}

    def _should_stop(provider_key: str) -> bool:
        return cancel_events[provider_key].is_set() or bool(
            global_stop is not None and global_stop.is_set()
        )

    def _abort_peer(provider_key: str) -> None:
        with state_lock:
            peer_flow = state["flows"].get(provider_key)
        if peer_flow is None:
            return
        ctrl = getattr(peer_flow, "_sms_callback", None)
        if ctrl is not None and hasattr(ctrl, "abort_peer_won"):
            ctrl.abort_peer_won()

    def _candidate(provider_key: str) -> None:
        _current_run.run_id = run_id
        flow = None
        claimed_winner = False
        try:
            cfg = Config()
            cfg.proxy = (options.get("proxy") or "").strip() or None
            mail = create_mail_provider(mail_source, db.get_mail_settings(), account)
            env_overrides = _session_env_overrides(
                options,
                should_stop=lambda: _should_stop(provider_key),
                register_password=shared_password,
            )
            env_overrides["_email_phase_lock"] = email_phase_lock
            env_overrides["CANCELLABLE_MAIL_WAIT"] = "1"
            flow = AuthFlow(
                cfg,
                sms_callback=_build_sms_callback(run_id, forced_provider_key=provider_key),
                env_overrides=env_overrides,
                on_password=_save_password_early,
                # 2FA is a single-account side effect; only the parent winner may bind it.
                on_session_ready=None,
                account_callback=_account_callback_for_flow,
            )
            with state_lock:
                state["flows"][provider_key] = flow
            logging.getLogger("registrar").info(
                f"[session-race:{provider_key}] 独立注册会话开始"
            )
            d, partial = _run_flow_to_dict(flow, mail, options)
            required_ok = (
                (not options.get("want_access_token", True) or bool(d.get("access_token")))
                and (not options.get("want_session_token", True) or bool(d.get("session_token")))
                and (not options.get("want_refresh_token", True) or bool(d.get("refresh_token")))
            )
            if partial or not required_ok:
                raise RuntimeError("会话未拿齐本次要求的凭证，不能成为竞速胜者")
            sms_ctrl = getattr(flow, "_sms_callback", None)
            actual_platform = str(getattr(sms_ctrl, "provider_key", "") or "").strip().lower()
            if sms_ctrl is None or not bool(getattr(sms_ctrl, "completed", False)):
                raise RuntimeError("会话未完成本轮手机验证，不能成为接码平台竞速胜者")
            if actual_platform != provider_key:
                raise RuntimeError(
                    f"会话接码平台不匹配: expected={provider_key} actual={actual_platform or 'none'}"
                )
            if _should_stop(provider_key):
                raise RuntimeError("注册会话因竞速对端完成/任务停止而中止")
            outcome = {
                "provider_key": provider_key,
                "flow": flow,
                "mail": mail,
                "cfg": cfg,
                "env_overrides": env_overrides,
                "d": d,
                "partial": partial,
                "tfa_box": {},
            }
            with state_lock:
                if state["winner"] is None:
                    state["winner"] = outcome
                    claimed_winner = True
                    peer_key = "smsbower" if provider_key == "herosms" else "herosms"
                    cancel_events[peer_key].set()
                    ready.set()
            if claimed_winner:
                logging.getLogger("registrar").info(
                    f"[session-race] 胜出平台={provider_key}，正在取消另一独立会话"
                )
                _abort_peer(peer_key)
            else:
                ctrl = getattr(flow, "_sms_callback", None)
                if ctrl is not None and hasattr(ctrl, "abort_peer_won"):
                    ctrl.abort_peer_won()
        except Exception as exc:
            peer_cancel = cancel_events[provider_key].is_set() and state.get("winner") is not None
            if flow is not None:
                ctrl = getattr(flow, "_sms_callback", None)
                if peer_cancel and ctrl is not None and hasattr(ctrl, "abort_peer_won"):
                    ctrl.abort_peer_won()
                elif ctrl is not None and hasattr(ctrl, "report_rt_result"):
                    ctrl.report_rt_result(bool(getattr(flow.result, "refresh_token", "")))
            with state_lock:
                state["errors"][provider_key] = str(exc)
                if len(state["errors"]) >= len(required) and state["winner"] is None:
                    ready.set()
            if not peer_cancel:
                logging.getLogger("registrar").warning(
                    f"[session-race:{provider_key}] 会话失败: {exc}"
                )
        finally:
            if flow is not None and not claimed_winner:
                _close_flow(flow)
            _current_run.run_id = None
            done_events[provider_key].set()

    threads = [
        threading.Thread(
            target=_candidate,
            args=(provider_key,),
            daemon=True,
            name=f"register-{run_id}-{provider_key}",
        )
        for provider_key in required
    ]
    for thread in threads:
        thread.start()

    while not ready.wait(0.25):
        if global_stop is not None and global_stop.is_set():
            for event in cancel_events.values():
                event.set()
            for provider_key in required:
                _abort_peer(provider_key)
            for event in done_events.values():
                event.wait()
            raise RuntimeError("双会话竞速因任务停止而中止")

    # Winner only reserves the result. Parent-side side effects start after both
    # attempt threads have observed cancellation and released HTTP/SMS resources.
    for event in done_events.values():
        event.wait()

    with state_lock:
        winner = state["winner"]
        errors = dict(state["errors"])
    if winner is not None:
        return winner
    detail = " | ".join(f"{key}: {errors.get(key, 'unknown')}" for key in required)
    raise RuntimeError("双会话竞速均失败: " + detail)


def _run_shared_mailbox_post_login_race(
    run_id: str,
    parent_flow: AuthFlow,
    options: dict,
) -> dict:
    """从一次共享邮箱登录快照派生 Hero/SmsBower 两条 Codex 分支。

    邮箱阶段只在 parent_flow 中执行一次；分支只复制完整认证 cookie，
    各自生成 PKCE/state 和 SMS activation。这样只返回“最新码”的接码链接
    不会被两个完整注册流程重复消费。
    """
    sms_cfg = db.get_sms_internal_config()
    provider_keys = {
        str(getattr(provider, "platform_key", "") or "").strip().lower()
        for provider in get_enabled_sms_providers(sms_cfg)
    }
    required = ("herosms", "smsbower")
    missing = [key for key in required if key not in provider_keys]
    if missing:
        raise RuntimeError("共享邮箱登录后双 Codex 分支缺少接码平台: " + ", ".join(missing))

    global_stop = options.get("stop_event")
    cancel_events = {key: threading.Event() for key in required}
    done_events = {key: threading.Event() for key in required}
    state_lock = threading.Lock()
    state = {"winner": None, "errors": {}, "flows": {}}

    def should_stop(key: str) -> bool:
        return cancel_events[key].is_set() or bool(global_stop is not None and global_stop.is_set())

    def abort_peer(key: str) -> None:
        with state_lock:
            peer = state["flows"].get(key)
        if peer is None:
            return
        ctrl = getattr(peer, "_sms_callback", None)
        if ctrl is not None and hasattr(ctrl, "abort"):
            ctrl.abort("peer_won")

    def candidate(key: str) -> None:
        _current_run.run_id = run_id
        branch = None
        claimed = False
        try:
            if should_stop(key):
                raise RuntimeError("分支在启动前已被取消")
            ctrl = _build_sms_callback(run_id, forced_provider_key=key)
            if ctrl is None:
                raise RuntimeError(f"无法创建强制接码 controller: {key}")
            branch = parent_flow.fork_authenticated_for_codex(
                sms_callback=ctrl,
                env_overrides={
                    **dict(parent_flow._env_overrides),
                    "_should_stop": lambda k=key: should_stop(k),
                    "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
                },
            )
            if should_stop(key):
                raise RuntimeError("分支在认证快照后已被取消")
            with state_lock:
                state["flows"][key] = branch
            logging.getLogger("registrar").info(
                "[mail-post-login:%s] 认证快照分支开始", key
            )
            result = branch.run_codex_from_authenticated_session()
            sms_ctrl = getattr(branch, "_sms_callback", None)
            if not result.refresh_token:
                raise RuntimeError("分支没有 refresh_token")
            if sms_ctrl is not None and hasattr(sms_ctrl, "report_rt_result"):
                sms_ctrl.report_rt_result(True)
            if should_stop(key):
                raise RuntimeError("分支因对端完成/任务停止而中止")
            outcome = {"provider_key": key, "flow": branch, "result": result}
            with state_lock:
                if state["winner"] is None:
                    state["winner"] = outcome
                    claimed = True
                    peer_key = "smsbower" if key == "herosms" else "herosms"
                    cancel_events[peer_key].set()
            if claimed:
                logging.getLogger("registrar").info(
                    "[mail-post-login] Codex 分支胜出 platform=%s", key
                )
                abort_peer(peer_key)
            else:
                if sms_ctrl is not None and hasattr(sms_ctrl, "abort"):
                    sms_ctrl.abort("peer_won")
        except Exception as exc:
            if branch is not None:
                ctrl = getattr(branch, "_sms_callback", None)
                if ctrl is not None:
                    try:
                        if state.get("winner") is not None:
                            ctrl.abort("peer_won")
                        elif hasattr(ctrl, "report_rt_result"):
                            ctrl.report_rt_result(False)
                    except Exception:
                        pass
            with state_lock:
                state["errors"][key] = str(exc)
            if not (cancel_events[key].is_set() and state.get("winner") is not None):
                logging.getLogger("registrar").warning(
                    "[mail-post-login:%s] 分支失败: %s", key, exc
                )
        finally:
            if branch is not None and not claimed:
                _close_flow(branch)
            done_events[key].set()
            _current_run.run_id = None

    threads = [
        threading.Thread(target=candidate, args=(key,), daemon=True, name=f"mail-codex-{run_id}-{key}")
        for key in required
    ]
    for thread in threads:
        thread.start()

    while True:
        if global_stop is not None and global_stop.is_set():
            for event in cancel_events.values():
                event.set()
            for key in required:
                abort_peer(key)
            break
        with state_lock:
            finished = len(state["errors"]) == len(required) or state["winner"] is not None
        if finished:
            break
        time.sleep(0.1)
    for event in done_events.values():
        event.wait()
    if global_stop is not None and global_stop.is_set():
        raise RuntimeError("共享邮箱登录后 Codex 双分支因任务停止而中止")
    with state_lock:
        winner = state["winner"]
        errors = dict(state["errors"])
    if winner is None:
        detail = " | ".join(f"{key}: {errors.get(key, 'unknown')}" for key in required)
        raise RuntimeError("共享邮箱登录后 Codex 双分支均失败: " + detail)
    result = winner["result"]
    output = {
        "email": result.email,
        "password": result.password,
        "session_token": parent_flow.result.session_token,
        "access_token": parent_flow.result.access_token,
        "device_id": parent_flow.result.device_id,
        "csrf_token": parent_flow.result.csrf_token,
        "id_token": result.id_token or parent_flow.result.id_token,
        "refresh_token": result.refresh_token,
        "cookie_header": parent_flow.result.cookie_header,
        "totp_secret": parent_flow.result.totp_secret,
    }
    # The result is fully copied above. Do not leave the winning curl session
    # alive after handing the credentials back to the parent registration flow.
    _close_flow(winner["flow"])
    return output


def _do_register(
    run_id: str,
    account: dict,
    options: dict,
    log_file: Path,
):
    """实际注册任务。

    options:
        want_access_token: bool
        want_session_token: bool
        want_refresh_token: bool
        proxy: Optional[str]
        otp_timeout: int
        allow_existing_login: bool
    """
    # 先认领本线程，再挂 handler —— 顺序不能反：中间要是有日志产生，
    # 没打标记的话会被广播到其他并发 run 的日志里去。
    _current_run.run_id = run_id

    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    with _lock:
        _run_sinks[run_id] = handler
    # 第一次需要的话提到 INFO 级别
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    email = account["email"]
    # 固化本轮邮箱来源。池化账号以 claim 时写进 account.kind 的类型为准；
    # 非池化占位账号同样带 kind。避免运行中切换全局邮箱配置后，本轮被另一
    # provider 接管，出现 Gmail 占位账号拿去初始化 Outlook 的 TOCTOU 串源。
    configured_mail_source = db.get_setting("mail_source", "outlook")
    account_mail_source = str(account.get("kind") or "").strip().lower()
    mail_source = account_mail_source or configured_mail_source
    # 要不要操作号池（mark_done / mark_failed / release）由 provider 声明的
    # pooled 决定。未知 kind 时保守当池化处理 —— 号池里真有这行的话
    # 至少不会漏掉状态回写，把号永远卡在 in_use。
    try:
        is_pooled = get_provider_class(mail_source).pooled
    except MailProviderError:
        is_pooled = True

    flow = None
    mail = None
    registration_succeeded = False
    mailbox_lock = _mailbox_group_lock(account)
    mailbox_lock_held = False
    try:
        _acquire_group_lock(mailbox_lock, options.get("stop_event"))
        mailbox_lock_held = mailbox_lock is not None
        prebuilt: dict = {}
        sms_mode = str(db.get_sms_internal_config().get("sms_mode") or "").strip().lower().replace("-", "_")
        # Gmail 和通用接码链接都可能让多个注册会话读取同一收件箱/relay URL；
        # 不能套用双完整会话竞速，否则会重复租号或消费同一 OTP。
        gmail_mail_source = mail_source in {"gmail", "gmail_link"}
        shared_mailbox_source = gmail_mail_source or mail_source == "mailbox_url"
        session_race_enabled = not options.get("disable_session_race")
        mailbox_post_login_race = (
            session_race_enabled
            and sms_mode == "session_race"
            and shared_mailbox_source
        )
        if session_race_enabled and sms_mode == "session_race" and is_pooled and not shared_mailbox_source:
            _emit_status(run_id, "phase", {"phase": "session_race", "message": "启动 HeroSMS / SmsBower 双独立会话"})
            logging.getLogger("registrar").info(
                "[session-race] 每个账号启动 HeroSMS、SmsBower 两个独立注册会话"
            )
            prebuilt = _run_session_race_auth(run_id, account, options, mail_source)
        elif session_race_enabled and sms_mode == "session_race" and (not is_pooled or shared_mailbox_source):
            logging.getLogger("registrar").warning(
                "[session-race] 共享邮箱接码链路只登录一次；随后派生 HeroSMS / SmsBower Codex 分支"
            )

        # 本次注册专属的配置覆盖。
        # ⚠️ 以前是写 os.environ + finally 还原，但 auto_loop 并发跑多个 worker，
        #    os.environ 是**进程全局**的：A 设的 OTP_TIMEOUT/WEBUI_ALLOW_LOGIN 会被
        #    B 读到，B 跑完还原成 A 之前的值，A 后半程就用上别人的配置了。
        #    现在整个 dict 直接传给 AuthFlow，只挂在实例上，谁都污染不到谁。
        env_overrides = dict(prebuilt.get("env_overrides") or {})
        # outlook 接码邮箱常被 OpenAI 走 passwordless_signup 流程（新号收码而非设密码），
        # auth_flow 会误判为"已有账号"分支 → 不设 WEBUI_ALLOW_LOGIN 会 fast-fail。
        # 单号 WebUI 场景下 fast-fail 没意义（批量跑才需要"跳过被识别的号"），故强制 ON。
        if not prebuilt:
            # A previous attempt may already have created the OpenAI account.
            # Always reuse the exact password that was persisted at POST 200;
            # generating a new one turns the next login into a deterministic 401.
            saved_password = ""
            try:
                saved = db.get_registered(email)
                saved_password = ((saved or {}).get("password") or "").strip()
            except Exception as exc:
                logging.getLogger("registrar").warning(
                    f"[register] 读取早存密码失败: {exc}"
                )
            env_overrides.update(_session_env_overrides(
                options,
                should_stop=(options.get("stop_event").is_set if options.get("stop_event") is not None else None),
                register_password=saved_password,
            ))
            if saved_password:
                logging.getLogger("registrar").info(
                    "[register] 检测到半成品记录，本轮强制复用早存密码"
                )
            if shared_mailbox_source:
                env_overrides["CANCELLABLE_MAIL_WAIT"] = "1"
            env_overrides["_on_email_created"] = lambda actual_email: (
                db.update_run_email(run_id, actual_email),
                _emit_status(run_id, "phase", {"phase": "mailbox_created", "email": actual_email}),
            )
        # 用户不要 refresh_token → 直接跳过 Codex OAuth（每次都失败浪费 ~10s + 一堆告警）
        if not prebuilt and not options.get("want_refresh_token", True):
            env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
        # PROXY 走 cfg.proxy，无需 env

        cfg = prebuilt.get("cfg") or Config()
        if not prebuilt:
            cfg.proxy = (options.get("proxy") or "").strip() or None

        # ─ 邮箱来源路由 ─
        # 原来是 if cf_temp / else outlook 的写死分支，加一种邮箱就得回来改。
        # 现在交给注册表工厂：provider 自己从 settings + account 里取需要的字段。
        mail = prebuilt.get("mail") or create_mail_provider(mail_source, db.get_mail_settings(), account)
        logging.getLogger("registrar").info(
            f"[register] 邮箱来源: {mail_source} ({mail.display_name})"
        )

        # ─ 2FA 绑定钩子：插在「拿到 session」和「Codex 授权」之间 ─
        #   主人指定的顺序：注册完 → 绑 2FA → Codex 授权 → 接码。
        #   2FA 必须有 access_token 才能打 mfa/enroll，而 at 只能从 get_auth_session 拿，
        #   所以这是唯一「已有 at 且 Codex 还没跑」的位置（见 auth_flow.py 那处注释）。
        #   钩子里绑成了就把结果存进 _tfa_box，run_register 返回后直接取，不再重绑。
        _tfa_box: dict = prebuilt.get("tfa_box") or {}

        def _persist_activated_totp(_flow, secret: str, factor_id: str = "") -> None:
            bound_email = (getattr(_flow.result, "email", "") or email).strip()
            db.save_totp_early(bound_email, secret, factor_id)
            _flow.result.totp_secret = secret
            _tfa_box.update({"secret": secret, "factor_id": factor_id})
            logging.getLogger("registrar").info(
                f"[register] TOTP 已即时落盘: {bound_email}（凭证待补）"
            )

        def _bind_2fa_hook(_flow, at: str) -> None:
            if not (getattr(_flow.result, "password", "") or "").strip():
                logging.getLogger("registrar").warning(
                    "[register] 勾了 2FA 但该号无密码，跳过绑定"
                )
                return
            from .two_factor import bind_totp_2fa_inline
            info = bind_totp_2fa_inline(
                _flow,
                at,
                on_activated=lambda secret, factor_id: _persist_activated_totp(
                    _flow, secret, factor_id
                ),
            )
            if info and info.get("secret"):
                _tfa_box.update(info)

        def _account_callback_for_flow(email: str) -> dict:
            """从数据库加载账号凭证（密码和 totp_secret）供 AuthFlow 登录时使用。

            用于既有账号登录场景：当服务端返回 mfa-challenge 时，AuthFlow 需要
            totp_secret 来计算 6 位动态码完成 2FA 验证。
            """
            try:
                data = db.get_registered(email)
                if data:
                    return {
                        "password": data.get("password", ""),
                        "totp_secret": data.get("totp_secret", ""),
                    }
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] account_callback 异常: {e}")
            return {}

        forced_sms_provider = "" if mailbox_post_login_race else ("smsbower" if gmail_mail_source else "")
        authenticated_session_hook = (
            (lambda authenticated_flow: _run_shared_mailbox_post_login_race(run_id, authenticated_flow, options))
            if mailbox_post_login_race else None
        )
        flow = prebuilt.get("flow") or AuthFlow(
            cfg,
            sms_callback=(
                None if mailbox_post_login_race
                else _build_sms_callback(run_id, forced_provider_key=forced_sms_provider)
            ),
            env_overrides=env_overrides,
            on_password=_save_password_early,
            on_session_ready=_bind_2fa_hook if options.get("want_2fa") else None,
            on_authenticated_session=authenticated_session_hook,
            account_callback=_account_callback_for_flow,
        )
        # Seed both the signup and all later Codex-login paths.  The latter read
        # result.password before consulting account_callback in some branches.
        if not prebuilt and env_overrides.get("REGISTER_PASSWORD"):
            flow.result.password = env_overrides["REGISTER_PASSWORD"]
        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logging.getLogger("registrar").info(f"[register] 开始: {email}")

        if prebuilt:
            d = dict(prebuilt["d"])
            partial = bool(prebuilt.get("partial"))
        else:
            d, partial = _run_flow_to_dict(flow, mail, options)

        # ─ 用户选项过滤：未勾选的字段从结果里抹掉，DB 只存用户想要的
        full = d
        d = {
            "email": full.get("email", ""),
            "password": full.get("password", ""),
        }
        if options.get("want_access_token", True):
            d["access_token"] = full.get("access_token", "")
        if options.get("want_session_token", True):
            d["session_token"] = full.get("session_token", "")
            d["cookie_header"] = full.get("cookie_header", "")  # 同样是浏览器注入用
        if options.get("want_refresh_token", True):
            d["refresh_token"] = full.get("refresh_token", "")
            d["id_token"] = full.get("id_token", "")

        # Keep the mailbox source used by this run with the credential. Export
        # happens after a user may have switched the global mailbox setting, so
        # it must use this run-local snapshot rather than current UI settings.
        d["mail_provider"] = mail_source
        for source_key, result_key in (
            ("relay_url", "mail_url"),
            ("password", "mail_password"),
            ("client_id", "mail_client_id"),
            ("refresh_token", "mail_refresh_token"),
        ):
            value = account.get(source_key)
            if value:
                d[result_key] = value

        # ─ 可选：绑定 TOTP 2FA（仅用户勾选 want_2fa 时才跑） ─
        #   正常情况上面的 on_session_ready 钩子已经在【Codex 授权之前】绑完了，
        #   这里只是兜底：钩子没跑到（run_register 中途抛异常走 partial 分支、
        #   或那时 access_token 还是空）时再补一次。
        #   兜底本身也是先快后慢两条路（见 two_factor.py 模块头）：
        #     快 bind_totp_2fa_inline —— 直接复用刚跑完注册的 flow + access_token，
        #        6.2s 搞定，零 PoW 零邮件（实测 2026-08-08 <测试号>@<自建域>
        #        四个请求全 200，mfa_enabled=true）。
        #     慢 bind_totp_2fa —— 新起 AuthFlow 重走 login 正式链，约 40s + 一次 PoW
        #        + 一封验证码邮件。只在快路径没成时兜底。
        #   失败仅告警、绝不废掉已注册成功的号；secret 一次性下发，成功即随 d 落库+推前端。
        if options.get("want_2fa") and (d.get("password") or "").strip():
            _emit_status(run_id, "phase", {"phase": "binding_2fa", "email": d.get("email")})
            try:
                from .two_factor import bind_totp_2fa, bind_totp_2fa_inline
                # 钩子（Codex 授权之前那次）已经绑好就直接用，别再打一遍 enroll
                tinfo = dict(_tfa_box) if _tfa_box.get("secret") else None
                if not tinfo:
                    tinfo = bind_totp_2fa_inline(
                        flow,
                        full.get("access_token", ""),
                        on_activated=lambda secret, factor_id: _persist_activated_totp(
                            flow, secret, factor_id
                        ),
                    )
                if not (tinfo and tinfo.get("secret")):
                    logging.getLogger("registrar").info(
                        "[register] 2FA 快路径未成，回落重走登录链..."
                    )
                    tinfo = bind_totp_2fa(
                        cfg, d.get("email", ""), d.get("password", ""),
                        mail_provider=mail, env_overrides=env_overrides,
                        on_activated=lambda secret, factor_id: (
                            db.save_totp_early(d.get("email", ""), secret, factor_id),
                            _tfa_box.update({"secret": secret, "factor_id": factor_id}),
                        ),
                    )
                if tinfo and tinfo.get("secret"):
                    d["totp_secret"] = tinfo["secret"]
                    d["totp_factor_id"] = tinfo.get("factor_id", "")
                    # The normal hook already persisted this.  Keep the same
                    # guarantee for either fallback binding path as well.
                    db.save_totp_early(
                        d.get("email", ""),
                        tinfo["secret"],
                        tinfo.get("factor_id", ""),
                    )
                    if flow is not None:
                        flow.result.totp_secret = tinfo["secret"]
                    logging.getLogger("registrar").info(
                        f"[register] 2FA 绑定成功 email={d.get('email')}"
                    )
                    _emit_status(run_id, "phase", {"phase": "2fa_bound", "email": d.get("email")})
                else:
                    logging.getLogger("registrar").warning(
                        "[register] 2FA 绑定未成功（账号仍有效，仅未绑 2FA）"
                    )
            except Exception as e:
                if getattr(e, "fatal", False):
                    raise
                logging.getLogger("registrar").warning(
                    f"[register] 2FA 绑定异常（账号仍有效）: {e}"
                )
        elif options.get("want_2fa"):
            logging.getLogger("registrar").warning(
                "[register] 勾选了 2FA 但该号无密码，跳过绑定"
            )

        # 落库
        db.save_registered(d)
        registration_succeeded = True
        # ⚠️ d 是**本轮内存里**的结果，它不一定知道这个号有密码：
        #    重跑一个之前设过密码的邮箱时，OpenAI 会认成已有账号 → passwordless_login
        #    → register_password 根本不执行 → d["password"] 是空的。
        #    但上一轮 save_password_early 存的密码还在库里，save_registered 刚刚
        #    已经把它保留下来了（空值不覆盖非空旧值）——「注册结果」页读 DB 显示正确，
        #    唯独跑完这一刻的 done 事件拿的是 d，前端 `v-if="lastRunResult.password"`
        #    判空 → 密码行连同「复制密码」「复制 email----password」两个按钮一起消失，
        #    主人会以为密码又丢了。所以这里从库里回读补回来。
        #    只在 d 里密码为空时查一次，正常路径零额外开销。
        if not (d.get("password") or "").strip():
            try:
                _saved = db.get_registered(d.get("email") or "")
                _pw = ((_saved or {}).get("password") or "").strip()
                if _pw:
                    d["password"] = _pw
                    logging.getLogger("registrar").info(
                        "[register] 本轮未设密码，沿用库中已存密码（上一轮 register_password 留下的）"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] 回读已存密码失败: {e}")
        # 非池化 provider 的 email 是虚拟占位（xxx_placeholder_N@placeholder.local），
        # 号池里根本没这行，不能去 mark。判据用 provider 的 pooled，不写死 kind。
        if is_pooled:
            db.mark_done(email)

        # ─ 可选：导出到 CPA / SUB2API 面板（仅勾选启用时才执行） ─
        if not options.get("skip_exports"):
            _try_export_to_panels(
                run_id,
                d,
                push_to_hub=bool(options.get("push_to_hub", True)),
            )

        result_summary = {
            "email": d.get("email"),
            # 密码走明文推给前端：token 只给长度是因为太长且必须点按钮复制，
            # 但密码是随机 16 位、用户注册完第一件事就是拿去登录，
            # 藏在「查看凭证」弹窗里等于每次都要多点两下。
            # 这是本机自用工具，SSE 只发给本地浏览器，不外传。
            "password": d.get("password") or "",
            "access_token_len": len(d.get("access_token") or ""),
            "session_token_len": len(d.get("session_token") or ""),
            "refresh_token_len": len(d.get("refresh_token") or ""),
            # 2FA secret 一次性下发、服务端取不回，明文推前端让用户当场导入验证器
            # （理由同密码；本机自用工具，SSE 只发本地浏览器）。未绑则为空串。
            "totp_secret": d.get("totp_secret") or "",
            "partial": partial,
        }
        _emit_status(run_id, "done", result_summary)
        logging.getLogger("registrar").info(
            f"[register] 完成 email={d.get('email')} "
            f"pw={d.get('password') or '(无)'} "
            f"at={result_summary['access_token_len']} "
            f"st={result_summary['session_token_len']} "
            f"rt={result_summary['refresh_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err, mail_source)
        logging.getLogger("registrar").error(f"[register] 失败 (category={category}): {err}")
        # ⚠️ 密码是在 register_password 里现生成的，只活在内存里。
        #    走到这里说明 save_registered 没执行过 —— 但 POST user/register 可能**已经成功**，
        #    OpenAI 那边账号连同这个密码已经建好了，只是后续步骤（发码/验证/建账户）挂了。
        #    不打出来的话这个号就成了谁也登不进去的孤儿。这里只写日志不落库，
        #    避免把没有任何 token 的半成品塞进「注册结果」表里。
        try:
            _pw = (flow.result.password or "").strip()
            if _pw:
                logging.getLogger("registrar").error(
                    f"[register] 该号已生成密码，请自行留存: {flow.result.email or email} / {_pw}"
                )
        except Exception:
            pass  # flow 还没建出来（异常发生在 AuthFlow 之前），没密码可救
        if category != "account":
            logging.getLogger("registrar").error(traceback.format_exc())
        # 非池化 provider 没有号池记录，不操作
        if is_pooled:
            if category == "network":
                db.release_unused(email)
                logging.getLogger("registrar").warning(
                    f"[register] {email} 判定为网络/环境错误，号已 release 回 available"
                )
            elif category in {"rate_limit", "sms_exhausted"}:
                db.defer_unused(email, defer_seconds=120)
                logging.getLogger("registrar").warning(
                    f"[register] {email} {category}，已延后 120s 再试"
                )
            else:
                db.mark_failed(email, f"[{category}] {err}")
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        # 临时邮箱 provider 需要显式确认/释放 SmsBower 激活；Outlook/CF 等
        # provider 的默认实现为空操作。
        try:
            if mail is not None and hasattr(mail, "finalize"):
                mail.finalize(registration_succeeded)
        except Exception as e:
            logging.getLogger("registrar").warning(f"[mail] 邮箱资源收尾失败: {e}")
        # SMS 验证通过不等于最终 RT 成功；在整个注册链结束时回灌真实 RT 结果。
        try:
            sms_ctrl = getattr(flow, "_sms_callback", None)
            if sms_ctrl is not None and hasattr(sms_ctrl, "report_rt_result"):
                sms_ctrl.report_rt_result(bool(getattr(flow.result, "refresh_token", "")))
        except Exception as e:
            logging.getLogger("registrar").warning(f"[sms] RT 结果回灌失败: {e}")
        _close_flow(flow)
        # env 覆盖现在只挂在 AuthFlow 实例上，随实例一起回收，无需还原。
        # 先从路由表移除，再关闭文件，避免并发日志写入已关闭的 sink。
        try:
            with _lock:
                _run_sinks.pop(run_id, None)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            _queue_put(q, None)  # sentinel: 流结束
        with _lock:
            done_event = _run_done_events.get(run_id)
        if done_event is not None:
            done_event.set()
        if mailbox_lock_held and mailbox_lock is not None:
            mailbox_lock.release()
        # 线程标记清掉。理论上线程跑完就回收了，但 threading.local 是绑在
        # 线程对象上的，万一以后换成线程池复用线程，残留的 run_id 会让下一个
        # 任务的日志全被投递到上一个 run 的（已关闭的）文件里去。
        _current_run.run_id = None


def _try_export_to_panels(run_id: str, cred: dict, *, push_to_hub: bool = True) -> None:
    """注册完成后可选地把凭证导出到 team-sso / CPA / SUB2API。

    - 任一目标的"启用"开关关闭时,该目标跳过(不发请求);两者都未启用时整段 no-op。
    - 任何异常都不抛,只 emit 日志/状态(不影响注册主流程)。
    """
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(f"[export] 读取配置失败: {e}")
        return

    cpa_enabled = bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = bool(cfg.get("sub2api", {}).get("enabled")) and push_to_hub
    team_sso_enabled = bool(cfg.get("team_sso", {}).get("enabled"))

    # team-sso consumes a versioned JSON credential payload. Persist it first so
    # network faults do not block the registration worker and are retried later.
    if team_sso_enabled:
        try:
            from . import team_sso_sync
            synced = team_sso_sync.enqueue_registered_account(cred.get("email") or "")
            if synced.get("queued"):
                logging.getLogger("registrar").info(
                    "[team-sso] 已加入 free 账号池同步队列 email=%s format=%s",
                    cred.get("email"), synced.get("format"),
                )
        except Exception as e:
            logging.getLogger("registrar").warning(
                "[team-sso] 加入同步队列失败 email=%s: %s", cred.get("email"), e
            )

    if not (cpa_enabled or sub2api_enabled):
        return

    from . import exporter  # 懒 import,避免未启用时强依赖

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    try:
        results = exporter.run_exports(
            cred,
            cpa_cfg=cfg.get("cpa") if cpa_enabled else None,
            sub2api_cfg=cfg.get("sub2api") if sub2api_enabled else None,
            log_fn=_log,
            token_update_fn=lambda tokens: db.update_registered_codex_tokens(
                cred.get("email") or "",
                refresh_token=tokens.get("refresh_token") or "",
                id_token=tokens.get("id_token") or "",
            ),
        )
    except Exception as e:
        _log(f"导出整体异常: {e}", "error")
        return

    # 汇总成一个事件给前端
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary})
    except Exception:
        pass


def _save_password_early(email: str, password: str) -> None:
    """AuthFlow 的 on_password 回调：密码在 OpenAI 侧一生效就落盘。

    以前密码只在流程**全部**跑通后才随 save_registered 一起入库，
    中间任何一步失败（实测最常见的是 OTP 超时）密码就只剩一行 ERROR 日志兜底 ——
    换台机器、日志轮转、或者干脆没人去翻，号就废了。

    这里存的是"有密码、无凭证"的半成品行，跑通后 save_registered 会用
    同一个 email 主键覆盖补全，不会多出一行对不上的记录。
    """
    log = logging.getLogger("registrar")
    try:
        db.save_password_early(email, password)
        log.info(f"[register] 密码已落盘: {email}（凭证待补）")
    except Exception as e:
        # 落盘失败不能影响注册；下面 except 里那行 ERROR 日志仍然是兜底
        log.warning(f"[register] 密码落盘失败，仅剩日志兜底: {e}")


def _build_sms_callback(
    run_id: str,
    forced_provider_key: str = "",
) -> Optional[PhoneCallbackController]:
    """根据 webui 配置创建 SMS 接码 controller。

    未启用接码或未配置 API key 时返回 None，flow 会回退到环境变量路径。
    log_fn 把租号/等码的状态推到 SSE 流，前端可见。
    """
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        if forced_provider_key:
            raise RuntimeError(f"强制接码平台 {forced_provider_key} 运行时已关闭 SMS")
        return None
    enabled_providers = get_enabled_sms_providers(cfg)
    enabled_keys = {
        str(getattr(provider, "platform_key", "") or "").strip().lower()
        for provider in enabled_providers
    }
    if forced_provider_key and forced_provider_key not in enabled_keys:
        raise RuntimeError(f"强制接码平台不可用: {forced_provider_key}")
    if not enabled_providers:
        logging.getLogger("registrar").warning("[sms] 已启用接码但没有可用平台，跳过")
        return None

    smslog = logging.getLogger("registrar")

    def _log(msg: str) -> None:
        # 既写日志、又通过 _emit_status 推 phase 事件给前端
        smslog.info(f"[sms] {msg}")
        try:
            lowered = msg.lower()
            phase = "sms_queue" if any(marker in lowered for marker in (
                "接码资源暂不可用", "暂时无可用容量", "封禁冷却",
            )) else "sms"
            _emit_status(run_id, "phase", {"phase": phase, "message": msg})
        except Exception:
            pass

    try:
        return PhoneCallbackController(
            provider_key=forced_provider_key or cfg["sms_provider"],
            config=cfg,
            service=cfg.get("sms_service") or "openai",
            country=cfg.get("sms_country") or "52",
            log_fn=_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
            # Session race freezes both platform credentials for this account.
            config_provider=None if forced_provider_key else db.get_sms_internal_config,
            forced_provider_key=forced_provider_key,
        )
    except Exception as e:
        if forced_provider_key:
            raise RuntimeError(f"创建强制接码 controller 失败({forced_provider_key}): {e}") from e
        smslog.warning(f"[sms] 创建接码 controller 失败: {e}")
        return None


def start_registration(account: dict, options: dict, observer=None) -> str:
    """启动一次注册任务，返回 run_id。"""
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, account["email"], str(log_file))

    max_queue_size = 2048
    try:
        max_queue_size = max(128, int(os.getenv("WEBUI_RUN_QUEUE_MAXSIZE", "2048")))
    except (TypeError, ValueError):
        pass
    q: queue.Queue = queue.Queue(maxsize=max_queue_size)
    with _lock:
        _run_queues[run_id] = q
        _run_done_events[run_id] = threading.Event()
        if observer is not None:
            _run_observers[run_id] = (
                lambda kind, payload, rid=run_id: observer(rid, kind, payload)
            )
        # Manual WebUI runs have no auto-loop waiter. Retain a bounded number
        # of completed events so long-running processes do not accumulate them.
        if len(_run_done_events) > 4096:
            for old_id, old_event in list(_run_done_events.items()):
                if old_event.is_set():
                    _run_done_events.pop(old_id, None)
                    if len(_run_done_events) <= 3072:
                        break

    th = threading.Thread(
        target=_do_register,
        args=(run_id, account, options, log_file),
        daemon=True,
        name=f"register-{run_id}",
    )
    th.start()
    return run_id


def get_run_queue(run_id: str) -> Optional[queue.Queue]:
    return _run_queues.get(run_id)


def wait_run_done(
    run_id: str,
    stop_event: Optional[threading.Event] = None,
    timeout: Optional[float] = None,
) -> Optional[bool]:
    """Wait for a run without polling SQLite.

    Returns ``None`` when the process has no event for this run (for example
    after a restart), so callers can retain their database-polling fallback.
    """
    with _lock:
        done_event = _run_done_events.get(run_id)
    if done_event is None:
        return None

    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
    while True:
        # stop_event requests cooperative cancellation inside AuthFlow. Keep
        # waiting until registrar has cleaned both attempts and finalized DB.
        if deadline is None:
            wait_for = 0.5
        else:
            wait_for = min(0.5, max(0.0, deadline - time.monotonic()))
            if wait_for <= 0:
                return False
        if done_event.wait(wait_for):
            with _lock:
                _run_done_events.pop(run_id, None)
            return True


def reauthorize_registered_account(
    email: str,
    *,
    proxy: str = "",
    stop_event: Optional[threading.Event] = None,
    timeout: float = 900,
) -> dict:
    """复用已有账号登录链，刷新 ChatGPT Session/Access Token。"""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return {"ok": False, "error": "缺少账号邮箱"}

    account = db.get_account(normalized)
    if not account:
        rows = db.list_registered_by_emails([normalized])
        registered = rows[0] if rows else {}
        account = {
            "email": normalized,
            "password": registered.get("mail_password") or "",
            "client_id": registered.get("mail_client_id") or "",
            "refresh_token": registered.get("mail_refresh_token") or "",
            "relay_url": registered.get("mail_url") or "",
            "kind": registered.get("mail_provider") or db.get_setting("mail_source", "outlook"),
        }
    if not account.get("email"):
        return {"ok": False, "error": "找不到账号对应的邮箱授权资料"}

    outcome = {"ok": False, "error": "授权任务未返回结果"}

    def observe(_run_id: str, kind: str, payload: dict) -> None:
        if kind == "done":
            outcome.update({"ok": True, "error": ""})
        elif kind == "error":
            outcome.update({"ok": False, "error": str(payload.get("message") or "授权失败")})

    options = {
        "want_access_token": True,
        "want_session_token": True,
        "want_refresh_token": True,
        "want_2fa": False,
        "allow_existing_login": True,
        "otp_timeout": 180,
        "proxy": str(proxy or "").strip(),
        "stop_event": stop_event,
        "disable_session_race": True,
        "skip_exports": True,
        "push_to_hub": False,
    }
    run_id = ""
    try:
        run_id = start_registration(account, options, observer=observe)
        finished = wait_run_done(run_id, stop_event=stop_event, timeout=timeout)
        if not finished:
            return {"ok": False, "error": "重新授权等待超时", "run_id": run_id}
        if not outcome["ok"]:
            return {**outcome, "run_id": run_id}
        refreshed = db.get_registered(normalized) or {}
        if not str(refreshed.get("access_token") or "").strip():
            return {"ok": False, "error": "重新授权完成但未保存 Access Token", "run_id": run_id}
        if not str(refreshed.get("refresh_token") or "").strip():
            return {"ok": False, "error": "重新授权完成但未保存 Codex Refresh Token", "run_id": run_id}
        return {"ok": True, "account": refreshed, "run_id": run_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "run_id": run_id}
    finally:
        if run_id:
            remove_run_observer(run_id)
            remove_run_queue(run_id)


def remove_run_queue(run_id: str) -> None:
    with _lock:
        _run_queues.pop(run_id, None)
