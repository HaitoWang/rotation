"""Durable background cancellation for failed or orphaned SMS rentals."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from sms_provider import SmsActivation, create_sms_provider

from . import db

logger = logging.getLogger("sms_cancel_dispatcher")

_wake = threading.Event()
_stop = threading.Event()
_thread_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def process_once(limit: int = 16) -> int:
    """Process one leased batch; exposed separately for deterministic tests."""
    # Provider HTTP retries can take over 90 seconds in the worst case.
    rows = db.claim_sms_activation_cancellations(limit=limit, lease_seconds=180)
    if not rows:
        return 0
    cfg = db.get_sms_internal_config()
    providers = {}
    for row in rows:
        platform = str(row.get("platform") or "").strip().lower()
        activation_id = str(row.get("activation_id") or "").strip()
        try:
            provider = providers.get(platform)
            if provider is None:
                provider = create_sms_provider(platform, cfg)
                providers[platform] = provider
            provider.current_activation = SmsActivation(
                activation_id,
                str(row.get("phone_number") or ""),
                "",
                {"acquired_at": float(row.get("acquired_at") or 0)},
            )
            if not provider.cancel(activation_id, record_failure=False):
                raise RuntimeError(provider.last_cancel_error or "取消接口未确认成功")
            db.complete_sms_activation_cleanup(platform, activation_id)
            logger.info(
                "[sms-cleanup] 取消成功 platform=%s activation_id=%s",
                platform,
                activation_id,
            )
        except Exception as exc:  # noqa: BLE001
            db.fail_sms_activation_cleanup(platform, activation_id, str(exc))
            logger.warning(
                "[sms-cleanup] 取消失败，将自动重试 platform=%s activation_id=%s: %s",
                platform,
                activation_id,
                exc,
            )
    return len(rows)


def _dispatch_loop() -> None:
    while not _stop.is_set():
        try:
            processed = process_once()
            if processed:
                continue
            _wake.wait(5.0)
            _wake.clear()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[sms-cleanup] 调度异常: %s", exc)
            _stop.wait(2.0)


def start_dispatcher() -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_dispatch_loop,
            daemon=True,
            name="sms-cancel-dispatcher",
        )
        _thread.start()


def wake_dispatcher() -> None:
    _wake.set()


def stop_dispatcher() -> None:
    _stop.set()
    _wake.set()
