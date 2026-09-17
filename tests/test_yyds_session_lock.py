from concurrent.futures import ThreadPoolExecutor
from time import sleep

from app.worker.locks import FILL_LOCK_KEY, yyds_session_lock_key
from app.yyds.client import account_session_lock


def test_account_session_lock_is_shared_and_serial():
    order: list[str] = []

    def work(tag: str):
        with account_session_lock("acct-a"):
            order.append(f"{tag}-in")
            sleep(0.05)
            order.append(f"{tag}-out")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(work, "one")
        sleep(0.01)
        second = pool.submit(work, "two")
        first.result()
        second.result()
    assert order[0].endswith("-in")
    assert order[1] == order[0].replace("-in", "-out")
    assert len(order) == 4
    with account_session_lock("acct-a"):
        with account_session_lock("acct-b"):
            order.append("nested-ok")
    assert order[-1] == "nested-ok"


def test_yyds_session_lock_key_stable_and_not_fill_key():
    first = yyds_session_lock_key("acct-a")
    assert first == yyds_session_lock_key("acct-a")
    assert first != yyds_session_lock_key("acct-b")
    assert first != FILL_LOCK_KEY
    assert first > 0
