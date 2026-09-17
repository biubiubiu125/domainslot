from pathlib import Path

from app.api import router

ROOT = Path(__file__).resolve().parents[1]


def test_no_yyds_domain_delete_api():
    paths = []
    for route in router.routes:
        methods = getattr(route, "methods", set()) or set()
        paths.append((tuple(sorted(methods)), route.path))
    assert ("DELETE", "/api/domains/{domain_id}") not in [(methods[0], path) if len(methods) == 1 else (methods, path) for methods, path in paths]
    for methods, path in paths:
        if "DELETE" in methods:
            assert "/api/domains/" not in path
            assert "wildcard" not in path
            assert path in {"/api/aliyun-accounts/{account_id}", "/api/yyds-accounts/{account_id}"}


def test_panel_has_no_yyds_domain_delete_button():
    html = (ROOT / "app/web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
    blob = html + js
    assert "删除 yyds 域名" not in blob
    assert "删除域名" not in blob
    assert "只监测和补位" in html
    assert "接收已关" in js
    assert "data-form-error" in html
    assert "data-form-error" in js
    assert "未知 / 不限" in js
    assert "used < 0" in js or "usedUnknown" in js
    assert "二验" in html
    assert "twofa_code" in js
    assert "used_wildcard == null" in js or "used_wildcard === null" in js


def test_compose_database_url_can_use_env():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "DATABASE_URL: ${DATABASE_URL:-" in text
    assert "DATABASE_URL: postgresql+psycopg://domainslot:domainslot@db:5432/domainslot" not in text


def test_compose_app_healthcheck_uses_healthz():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app_block = text.split("  app:")[1]
    assert "healthcheck:" in app_block
    assert "/api/healthz" in app_block


def test_dockerfile_healthcheck_uses_healthz():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/api/healthz" in text
    paths = {getattr(route, "path", None) for route in router.routes}
    assert "/api/healthz" in paths


def test_default_verify_window_covers_dns_propagation():
    from app.config import Settings

    settings = Settings.model_construct()
    assert settings.verify_attempts * settings.verify_retry_seconds >= 180
