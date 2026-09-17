from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.db.session import get_session, init_db
from app.worker.events import redact
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
    while not _stop.is_set():
        forced = _scan.is_set()
        _scan.clear()
        try:
            _run_cycle(settings, force_all=forced)
            _last_cycle = datetime.now(timezone.utc)
            _last_error = None
        except Exception as exc:  # noqa: BLE001
            _last_error = redact(str(exc))[:500]
            logger.exception("worker cycle failed")
        _scan.wait(timeout=max(5.0, float(settings.yyds_poll_seconds)))


def _run_cycle(settings: Settings, force_all: bool) -> None:
    session = get_session()
    try:
        shrink_ids = poll_yyds_accounts(session, settings)
        session.commit()
        poll_aliyun_accounts(session, settings, force=force_all)
        session.commit()
        prefer_queue = list(dict.fromkeys(shrink_ids))
        safety = 0
        while safety < 50:
            safety += 1
            prefer = prefer_queue[0] if prefer_queue else None
            did = fill_available(session, settings, prefer_yyds_id=prefer)
            session.commit()
            if did:
                continue
            if prefer_queue:
                prefer_queue.pop(0)
                continue
            break
    finally:
        session.close()
