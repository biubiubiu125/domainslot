from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.db.models import EventLog

logger = logging.getLogger("domainslot")

SECRET_RE = re.compile(
    r"(yyds-xiaolajiao-[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._-]+|LTAI[A-Za-z0-9]+|eyJ[A-Za-z0-9._-]+)",
    re.I,
)


def redact(text: str | None) -> str:
    if not text:
        return ""
    cleaned = SECRET_RE.sub("[redacted]", text)
    cleaned = re.sub(r"(password|secret|token|cookie|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", cleaned, flags=re.I)
    return cleaned[:2000]


def add_event(
    session: Session,
    *,
    level: str,
    code: str,
    message: str,
    domain_name: str | None = None,
    aliyun_account_id: uuid.UUID | None = None,
    yyds_account_id: uuid.UUID | None = None,
) -> None:
    safe = redact(message)
    session.add(
        EventLog(
            level=level,
            code=code,
            message=safe,
            domain_name=domain_name,
            aliyun_account_id=aliyun_account_id,
            yyds_account_id=yyds_account_id,
        )
    )
    log_fn = logger.error if level == "error" else logger.info
    log_fn("%s %s", code, safe)
