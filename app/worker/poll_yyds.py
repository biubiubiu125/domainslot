from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.crypto import encrypt_json, encrypt_text
from app.db.models import Domain, YydsAccount, YydsDomainSnapshot
from app.domainutil import normalize_domain
from app.worker.clients import yyds_client
from app.worker.events import add_event
from app.worker.logic import STATUS_USED, diff_domain_sets, status_after_yyds_removed
from app.yyds.client import YydsError, extract_quota


def _now() -> datetime:
    return datetime.now(timezone.utc)


def persist_yyds_session(settings: Settings, account: YydsAccount, client) -> None:
    account.cookies_enc = encrypt_json(settings.secret_key, client.export_cookies())
    if client.access_token:
        account.access_token_enc = encrypt_text(settings.secret_key, client.access_token)


def poll_yyds_accounts(session: Session, settings: Settings) -> list[str]:
    shrink_ids: list[str] = []
    accounts = list(
        session.scalars(select(YydsAccount).where(YydsAccount.enabled.is_(True)).order_by(YydsAccount.sort_order, YydsAccount.created_at)).all()
    )
    for account in accounts:
        if poll_one_yyds_account(session, settings, account):
            shrink_ids.append(str(account.id))
        session.commit()
    return shrink_ids


def poll_one_yyds_account(session: Session, settings: Settings, account: YydsAccount) -> bool:
    now = _now()
    account.last_poll_at = now
    client = yyds_client(settings, account)
    try:
        client.ensure_session()
        me = client.get_me()
        try:
            quota_payload = client.get_quota()
        except YydsError:
            quota_payload = {}
        domains = client.list_domains()
        rules_ok = False
        try:
            rules = client.list_wildcard_rules()
            rules_ok = True
        except YydsError:
            rules = []
        persist_yyds_session(settings, account, client)
    except YydsError as exc:
        account.login_error = str(exc)[:500]
        add_event(
            session,
            level="error",
            code="yyds_login_failed",
            message=f"yyds 账号 {account.name} 登录或拉列表失败: {exc}",
            yyds_account_id=account.id,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        account.login_error = str(exc)[:500]
        add_event(
            session,
            level="error",
            code="yyds_login_failed",
            message=f"yyds 账号 {account.name} 登录或拉列表失败: {exc}",
            yyds_account_id=account.id,
        )
        return False
    finally:
        client.close()

    quota = extract_quota(me, quota_payload, rules, [item.__dict__ for item in domains])
    account.login_error = None
    account.plan_name = quota.get("plan_name")
    account.max_wildcard = quota.get("max_wildcard")
    account.used_wildcard = len(rules) if rules_ok else int(quota.get("used_wildcard") or 0)
    account.max_domains = quota.get("max_domains")
    account.used_domains = len(domains)
    account.last_success_at = now

    current_names: list[str] = []
    current_map: dict[str, object] = {}
    for item in domains:
        try:
            name = normalize_domain(item.domain)
        except ValueError:
            continue
        current_names.append(name)
        current_map[name] = item

    snapshot = session.scalar(select(YydsDomainSnapshot).where(YydsDomainSnapshot.yyds_account_id == account.id))
    previous: list[str] = []
    if snapshot:
        try:
            loaded = json.loads(snapshot.names_json or "[]")
            previous = [str(item) for item in loaded]
        except json.JSONDecodeError:
            previous = []

    diff = diff_domain_sets(previous, current_names)
    shrunk = False

    for name in sorted(diff.added):
        item = current_map.get(name)
        row = session.scalar(select(Domain).where(Domain.name == name))
        if row is None:
            add_event(
                session,
                level="info",
                code="yyds_only_domain",
                message=f"yyds 账号 {account.name} 有域名 {name}，本地库存没有对应阿里云记录",
                domain_name=name,
                yyds_account_id=account.id,
            )
            continue
        row.yyds_account_id = account.id
        row.yyds_domain_id = getattr(item, "id", None)
        row.last_seen_on_yyds_at = now
        row.filling_at = None
        if row.status != "error":
            row.status = STATUS_USED
            row.error_reason = None
        add_event(
            session,
            level="info",
            code="yyds_bound",
            message=f"域名 {name} 已出现在 yyds 账号 {account.name}",
            domain_name=name,
            yyds_account_id=account.id,
        )

    for name in sorted(diff.removed):
        shrunk = True
        row = session.scalar(select(Domain).where(Domain.name == name))
        if row is None:
            continue
        if row.yyds_account_id == account.id:
            row.status = status_after_yyds_removed(row.status)
            row.yyds_account_id = None
            row.yyds_domain_id = None
            row.filling_at = None
            add_event(
                session,
                level="info",
                code="yyds_removed",
                message=f"监测到 yyds 账号 {account.name} 少了域名 {name}，本地保持已使用，准备补另一条",
                domain_name=name,
                yyds_account_id=account.id,
            )

    for name, item in current_map.items():
        row = session.scalar(select(Domain).where(Domain.name == name))
        if row is None:
            continue
        row.yyds_account_id = account.id
        row.yyds_domain_id = getattr(item, "id", None)
        row.last_seen_on_yyds_at = now

    names_json = json.dumps(sorted(set(current_names)), ensure_ascii=False)
    if snapshot is None:
        session.add(YydsDomainSnapshot(yyds_account_id=account.id, names_json=names_json))
    else:
        snapshot.names_json = names_json

    if account.first_synced_at is None:
        account.first_synced_at = now

    return shrunk
