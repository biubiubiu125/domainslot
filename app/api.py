from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app import __version__
from app.config import get_settings
from app.crypto import encrypt_text, hash_password, new_session_token, verify_password
from app.db.models import AdminAuth, AliyunAccount, Domain, EventLog, YydsAccount
from app.db.session import db_session
from app.worker.logic import USER_STATUSES, AccountView, has_wildcard_slot, overview_notice, select_fill_account
from app.worker.runtime import request_scan, worker_status

router = APIRouter()
COOKIE_NAME = "domainslot_session"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def get_db():
    yield from db_session()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _current_admin(session: Session) -> AdminAuth:
    admin = session.scalar(select(AdminAuth).limit(1))
    settings = get_settings()
    if admin is None:
        if not settings.panel_password:
            raise HTTPException(500, "未配置 PANEL_PASSWORD")
        admin = AdminAuth(password_hash=hash_password(settings.panel_password), session_token=None)
        session.add(admin)
        session.flush()
    return admin


def require_login(request: Request, session: Session = Depends(get_db)) -> AdminAuth:
    admin = _current_admin(session)
    token = request.cookies.get(COOKIE_NAME)
    if not token or not admin.session_token or token != admin.session_token:
        raise HTTPException(401, "未登录")
    return admin


class LoginBody(BaseModel):
    password: str


class AliyunBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    access_key_id: str = Field(min_length=1, max_length=128)
    access_key_secret: str | None = None
    enabled: bool = True


class YydsBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=255)
    password: str | None = None
    sort_order: int = 100
    receive_enabled: bool = True
    enabled: bool = True


class DomainStatusBody(BaseModel):
    status: str


@router.post("/api/login")
def login(body: LoginBody, response: Response, session: Session = Depends(get_db)):
    admin = _current_admin(session)
    settings = get_settings()
    ok = verify_password(body.password, admin.password_hash)
    if not ok and settings.panel_password and body.password == settings.panel_password:
        admin.password_hash = hash_password(body.password)
        ok = True
    if not ok:
        raise HTTPException(401, "密码错误")
    token = new_session_token()
    admin.session_token = token
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return {"ok": True}


@router.post("/api/logout")
def logout(response: Response, session: Session = Depends(get_db), admin: AdminAuth = Depends(require_login)):
    admin.session_token = None
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/api/health")
def health(session: Session = Depends(get_db)):
    db_ok = True
    try:
        session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    worker = worker_status()
    return {
        "ok": db_ok and worker.get("alive") == "yes",
        "version": __version__,
        "db": db_ok,
        "worker": worker,
    }


@router.get("/api/overview")
def overview(session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    unused = session.scalar(select(func.count()).select_from(Domain).where(Domain.status == "unused")) or 0
    used = session.scalar(select(func.count()).select_from(Domain).where(Domain.status == "used")) or 0
    error = session.scalar(select(func.count()).select_from(Domain).where(Domain.status == "error")) or 0
    yyds_accounts = list(session.scalars(select(YydsAccount).order_by(YydsAccount.sort_order)).all())
    aliyun_accounts = list(session.scalars(select(AliyunAccount).order_by(AliyunAccount.created_at)).all())
    views = [
        AccountView(
            id=str(item.id),
            sort_order=item.sort_order,
            receive_enabled=item.receive_enabled,
            enabled=item.enabled,
            login_ok=not item.login_error,
            max_wildcard=item.max_wildcard,
            used_wildcard=item.used_wildcard or 0,
        )
        for item in yyds_accounts
    ]
    fillable = select_fill_account(views) is not None
    alerts: list[str] = []
    notice = overview_notice(int(unused), fillable)
    if notice:
        alerts.append(notice)
    for item in yyds_accounts:
        if item.login_error:
            alerts.append(f"yyds 账号 {item.name} 登录失败")
    now = datetime.now(timezone.utc)
    for item in aliyun_accounts:
        if item.last_error:
            alerts.append(f"阿里云账号 {item.name}：{item.last_error}")
        throttle_until = item.throttle_until
        if throttle_until is not None and throttle_until.tzinfo is None:
            throttle_until = throttle_until.replace(tzinfo=timezone.utc)
        if throttle_until and throttle_until > now:
            alerts.append(f"阿里云账号 {item.name} 正在限流")
    last_event = session.scalar(select(EventLog).order_by(EventLog.created_at.desc()).limit(1))
    worker = worker_status()
    return {
        "version": __version__,
        "unused": unused,
        "used": used,
        "error": error,
        "alerts": alerts,
        "worker": worker,
        "last_event": _event_dict(last_event) if last_event else None,
        "yyds": [_yyds_dict(item, include_domains=True, session=session) for item in yyds_accounts],
        "aliyun": [_aliyun_dict(item) for item in aliyun_accounts],
    }


@router.get("/api/aliyun-accounts")
def list_aliyun(session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    rows = session.scalars(select(AliyunAccount).order_by(AliyunAccount.created_at)).all()
    return [_aliyun_dict(item) for item in rows]


@router.post("/api/aliyun-accounts")
def create_aliyun(body: AliyunBody, session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    if not body.access_key_secret:
        raise HTTPException(400, "请填写 AccessKey Secret")
    settings = get_settings()
    row = AliyunAccount(
        name=body.name.strip(),
        access_key_id=body.access_key_id.strip(),
        access_key_secret_enc=encrypt_text(settings.secret_key, body.access_key_secret.strip()),
        enabled=body.enabled,
    )
    session.add(row)
    session.flush()
    request_scan()
    return _aliyun_dict(row)


@router.patch("/api/aliyun-accounts/{account_id}")
def update_aliyun(account_id: UUID, body: AliyunBody, session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    row = session.get(AliyunAccount, account_id)
    if row is None:
        raise HTTPException(404, "阿里云账号不存在")
    row.name = body.name.strip()
    row.access_key_id = body.access_key_id.strip()
    row.enabled = body.enabled
    if body.access_key_secret:
        row.access_key_secret_enc = encrypt_text(get_settings().secret_key, body.access_key_secret.strip())
    return _aliyun_dict(row)


@router.delete("/api/aliyun-accounts/{account_id}")
def delete_aliyun(account_id: UUID, session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    row = session.get(AliyunAccount, account_id)
    if row is None:
        raise HTTPException(404, "阿里云账号不存在")
    session.delete(row)
    return {"ok": True}


@router.get("/api/yyds-accounts")
def list_yyds(session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    rows = session.scalars(select(YydsAccount).order_by(YydsAccount.sort_order, YydsAccount.created_at)).all()
    return [_yyds_dict(item, include_domains=True, session=session) for item in rows]


@router.post("/api/yyds-accounts")
def create_yyds(body: YydsBody, session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    if not body.password:
        raise HTTPException(400, "请填写 yyds 密码")
    settings = get_settings()
    row = YydsAccount(
        name=body.name.strip(),
        username=body.username.strip(),
        password_enc=encrypt_text(settings.secret_key, body.password),
        sort_order=body.sort_order,
        receive_enabled=body.receive_enabled,
        enabled=body.enabled,
    )
    session.add(row)
    session.flush()
    request_scan()
    return _yyds_dict(row)


@router.patch("/api/yyds-accounts/{account_id}")
def update_yyds(account_id: UUID, body: YydsBody, session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    row = session.get(YydsAccount, account_id)
    if row is None:
        raise HTTPException(404, "yyds 账号不存在")
    row.name = body.name.strip()
    row.username = body.username.strip()
    row.sort_order = body.sort_order
    row.receive_enabled = body.receive_enabled
    row.enabled = body.enabled
    if body.password:
        row.password_enc = encrypt_text(get_settings().secret_key, body.password)
        row.cookies_enc = None
        row.access_token_enc = None
        row.login_error = None
    request_scan()
    return _yyds_dict(row)


@router.delete("/api/yyds-accounts/{account_id}")
def delete_yyds(account_id: UUID, session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    row = session.get(YydsAccount, account_id)
    if row is None:
        raise HTTPException(404, "yyds 账号不存在")
    session.delete(row)
    return {"ok": True}


@router.get("/api/domains")
def list_domains(session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    rows = session.scalars(
        select(Domain)
        .options(selectinload(Domain.aliyun_account), selectinload(Domain.yyds_account))
        .order_by(Domain.registration_at.asc().nullslast(), Domain.created_at.asc())
    ).all()
    return [_domain_dict(item) for item in rows]


@router.patch("/api/domains/{domain_id}")
def update_domain(domain_id: UUID, body: DomainStatusBody, session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    if body.status not in USER_STATUSES:
        raise HTTPException(400, "状态只能是 unused / used / error")
    row = session.get(Domain, domain_id)
    if row is None:
        raise HTTPException(404, "域名不存在")
    row.status = body.status
    if body.status != "error":
        row.error_reason = None
    row.filling_at = None
    request_scan()
    return _domain_dict(row)


@router.get("/api/events")
def list_events(session: Session = Depends(get_db), _: AdminAuth = Depends(require_login)):
    rows = session.scalars(select(EventLog).order_by(EventLog.created_at.desc()).limit(200)).all()
    return [_event_dict(item) for item in rows]


@router.post("/api/scan")
def scan(_: AdminAuth = Depends(require_login)):
    request_scan()
    return {"ok": True}


def _aliyun_dict(item: AliyunAccount) -> dict:
    key = item.access_key_id
    masked = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
    return {
        "id": str(item.id),
        "name": item.name,
        "access_key_id": key,
        "access_key_id_masked": masked,
        "enabled": item.enabled,
        "first_synced_at": _iso(item.first_synced_at),
        "last_poll_at": _iso(item.last_poll_at),
        "last_success_at": _iso(item.last_success_at),
        "throttle_until": _iso(item.throttle_until),
        "last_error": item.last_error,
    }


def _yyds_dict(item: YydsAccount, include_domains: bool = False, session: Session | None = None) -> dict:
    full = has_wildcard_slot(item.max_wildcard, item.used_wildcard or 0) is False
    data = {
        "id": str(item.id),
        "name": item.name,
        "username": item.username,
        "sort_order": item.sort_order,
        "receive_enabled": item.receive_enabled,
        "enabled": item.enabled,
        "login_error": item.login_error,
        "plan_name": item.plan_name,
        "max_wildcard": item.max_wildcard,
        "used_wildcard": item.used_wildcard,
        "max_domains": item.max_domains,
        "used_domains": item.used_domains,
        "wildcard_full": full,
        "last_poll_at": _iso(item.last_poll_at),
        "last_success_at": _iso(item.last_success_at),
        "domains": [],
    }
    if include_domains and session is not None:
        names = session.scalars(select(Domain.name).where(Domain.yyds_account_id == item.id).order_by(Domain.name)).all()
        data["domains"] = list(names)
    return data


def _domain_dict(item: Domain) -> dict:
    return {
        "id": str(item.id),
        "name": item.name,
        "display_name": item.display_name or item.name,
        "status": item.status,
        "from_first_snapshot": item.from_first_snapshot,
        "registration_at": _iso(item.registration_at),
        "error_reason": item.error_reason,
        "aliyun_account_id": str(item.aliyun_account_id) if item.aliyun_account_id else None,
        "aliyun_account_name": item.aliyun_account.name if item.aliyun_account else None,
        "yyds_account_id": str(item.yyds_account_id) if item.yyds_account_id else None,
        "yyds_account_name": item.yyds_account.name if item.yyds_account else None,
        "yyds_domain_id": item.yyds_domain_id,
        "filling": item.filling_at is not None,
        "last_seen_on_aliyun_at": _iso(item.last_seen_on_aliyun_at),
        "last_seen_on_yyds_at": _iso(item.last_seen_on_yyds_at),
    }


def _event_dict(item: EventLog) -> dict:
    return {
        "id": str(item.id),
        "created_at": _iso(item.created_at),
        "level": item.level,
        "code": item.code,
        "message": item.message,
        "domain_name": item.domain_name,
    }
