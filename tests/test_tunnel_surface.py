"""經 quick tunnel 對外的 HTTP 路由（2026-10-17）。

整個 app 都經 cloudflared quick tunnel 公開，「🔍 看大字版」按鈕又讓更多人看到主機名，所以：
- FastAPI 預設的 /docs、/redoc、/openapi.json（連同 /docs/oauth2-redirect）不掛；
- 除了 jobs_router 自己的路由（它擋經 proxy 來的請求，見 test_jobs_router），其他路由（含 Mount）一條不多一條不少。
"""

from fastapi.testclient import TestClient

# LINE 平台與家人手機會經 tunnel 用到的路由。新增 HTTP 路由前，先想清楚它要不要經 tunnel 對外，再改這份清單。
_PUBLIC_ROUTES = {
    ("/callback", "POST"),
    ("/health", "GET"),
    ("/static/img/{filename}", "GET"),
    ("/card/{token}", "GET"),
}
_DOCS_PATHS = ("/docs", "/docs/", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


def test_fastapi_docs_are_not_mounted():
    import main

    app = main.app  # 在函式裡才取：test_organic_correction 會 reload main
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    client = TestClient(app)  # 不用 with：with 會跑 lifespan，啟動背景執行緒
    for path in _DOCS_PATHS:
        assert client.get(path).status_code == 404, path


def test_routes_outside_jobs_router_are_exactly_what_line_needs():
    import main

    routes = set()
    for route in main.app.routes:
        # jobs_router 的路由有沒有掛看 JOBS_ROUTES_ENABLED，另由 test_jobs_router 管。用 endpoint 所屬模組認，
        # 不用路徑前綴：直接寫在 main.py、路徑剛好以 /jobs 開頭的路由也要列出來
        if getattr(getattr(route, "endpoint", None), "__module__", "") == "jobs_router":
            continue
        # Mount 之類沒有 methods 的也要列出來（記成 None），才不會默默多一個對外的入口
        for method in sorted(getattr(route, "methods", None) or {None}):
            routes.add((route.path, method))
    assert routes == _PUBLIC_ROUTES
