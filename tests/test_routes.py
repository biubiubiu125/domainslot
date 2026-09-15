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
