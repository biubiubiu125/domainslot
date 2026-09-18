from pathlib import Path

from app.api import router

ROOT = Path(__file__).resolve().parents[1]


def test_yyds_domain_delete_api_exists():
    paths = []
    for route in router.routes:
        methods = getattr(route, "methods", set()) or set()
        paths.append((set(methods), route.path))
    assert any("DELETE" in methods and path == "/api/domains/{domain_id}/yyds" for methods, path in paths)
    assert not any("DELETE" in methods and path == "/api/domains/{domain_id}" for methods, path in paths)


def test_panel_has_yyds_domain_delete_button():
    html = (ROOT / "app/web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
    assert "从 yyds 删除" in js
    assert "data-del-yyds-domain" in js
    assert "重试清理解析" in js
    assert "阿里云解析清理失败" in js
    handler = js[js.rfind("data-del-yyds-domain"):]
    assert "catch" in handler
    assert "alert(" in handler
    assert "A/CNAME" in handler or "冲突 A" in handler or "A、CNAME" in handler
    assert "只从本监控移除这个 yyds 账号" in js
    assert "没有删除按钮" not in html
    assert "只监测和补位" not in html
    assert "接收已关" in js
    assert "data-form-error" in html
    assert "data-form-error" in js
    assert "未知 / 不限" in js
    assert "used < 0" in js or "usedUnknown" in js
    assert "二验" in html
    assert "twofa_code" in js
    assert "used_wildcard == null" in js or "used_wildcard === null" in js


def test_account_dialogs_cancel_without_validation_and_disable_autofill():
    html = (ROOT / "app/web/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
    assert html.count('data-close-dialog') >= 2
    assert 'type="submit" value="cancel"' not in html
    assert 'autocomplete="off"' in html
    assert 'autocomplete="new-password"' in html
    assert 'data-no-autofill' in html
    assert 'data-close-dialog' in js
    assert 'autocomplete="current-password"' in html


def test_yyds_account_chips_do_not_duplicate_receive_closed():
    js = (ROOT / "app/web/static/app.js").read_text(encoding="utf-8")
    assert 'chip("接收已关"' in js
    assert '!item.receive_enabled ? "接收已关"' not in js


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
