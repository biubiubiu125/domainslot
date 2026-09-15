from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.db.session import get_session, init_db
from app.worker.fill import fill_available
from app.worker.poll_aliyun import poll_aliyun_accounts
from app.worker.poll_yyds import poll_yyds_accounts

logger = logging.getLogger("domainslot.worker")

_stop = threading.Event()
_scan = threading.Event()
_thread: threading.Thread | None = None
_last_cycle: datetime | None = None
_last_error: str | None = None


def worker_status() -> dict[str, str | None]:
    return {
        "alive": "yes" if _thread is not None and _thread.is_alive() else "no",
        "last_cycle": _last_cycle.isoformat() if _last_cycle else None,
        "last_error": _last_error,
    }


def request_scan() -> None:
    _scan.set()


def start_worker() -> None:
    global _thread
    init_db()
    _stop.clear()
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="domainslot-worker", daemon=True)
    _thread.start()


def stop_worker() -> None:
    _stop.set()
    _scan.set()
    if _thread:
        _thread.join(timeout=5)


def _loop() -> None:
    global _last_cycle, _last_error
    settings = get_settings()
    last_aliyun = 0.0
    while not _stop.is_set():
        forced = _scan.is_set()
        _scan.clear()
        try:
            _run_cycle(settings, force_aliyun=forced or (time.time() - last_aliyun >= settings.aliyun_poll_seconds))
            if forced or (time.time() - last_aliyun >= settings.aliyun_poll_seconds):
                last_aliyun = time.time()
            _last_cycle = datetime.now(timezone.utc)
            _last_error = None
        except Exception as exc:  # noqa: BLE001
            _last_error = str(exc)[:500]
            logger.exception("worker cycle failed")
        _scan.wait(timeout=max(5.0, float(settings.yyds_poll_seconds)))


def _run_cycle(settings: Settings, force_aliyun: bool) -> None:
    session = get_session()
    try:
        shrink_ids = poll_yyds_accounts(session, settings)
        session.commit()
        if force_aliyun:
            poll_aliyun_accounts(session, settings)
            session.commit()
        prefer = shrink_ids[0] if shrink_ids else None
        safety = 0
        while safety < 50:
            safety += 1
            did = fill_available(session, settings, prefer_yyds_id=prefer)
            session.commit()
            if not did:
                break
            prefer = None
    finally:
        session.close()
