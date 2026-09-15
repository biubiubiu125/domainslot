from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import router
from app.config import STATIC_DIR, get_settings
from app.crypto import hash_password
from app.db.models import AdminAuth
from app.db.session import get_session, init_db
from app.worker.runtime import start_worker, stop_worker
from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("domainslot")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if not settings.secret_key or len(settings.secret_key) < 16:
        raise RuntimeError("请设置长度至少 16 的 SECRET_KEY")
    if not settings.panel_password:
        raise RuntimeError("请设置 PANEL_PASSWORD")
    init_db()
    session = get_session()
    try:
        admin = session.scalar(select(AdminAuth).limit(1))
        if admin is None:
            session.add(AdminAuth(password_hash=hash_password(settings.panel_password)))
            session.commit()
    finally:
        session.close()
    start_worker()
    logger.info("yyds邮箱域名监测 %s 已启动", __version__)
    yield
    stop_worker()


app = FastAPI(title="yyds邮箱域名监测", version=__version__, lifespan=lifespan)
app.include_router(router)


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(500, "前端文件缺失")
    return FileResponse(index_path)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
