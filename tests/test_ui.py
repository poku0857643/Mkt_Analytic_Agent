"""The browser UI is served by the API and locked down with security headers."""
import re

import pytest


def test_index_served_with_csp(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Marketing Answers</title>" in response.text

    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_index_needs_no_key(client):
    # The page itself is public; data endpoints still require X-API-Key.
    assert client.get("/").status_code == 200
    assert client.post("/ask", json={"question": "hello there"}).status_code == 401


@pytest.mark.parametrize(
    "path, content_type",
    [
        ("/static/app.js", "javascript"),
        ("/static/styles.css", "text/css"),
        ("/static/vendor/echarts/echarts.min.js", "javascript"),
    ],
)
def test_assets_served(client, path, content_type):
    response = client.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_index_has_no_inline_or_external_code(client):
    html = client.get("/").text
    # CSP blocks inline scripts/styles, so the page must not rely on any.
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html
    assert not re.search(r"\son[a-z]+=", html)
    assert not re.search(r"(src|href)=\"https?://", html)


def test_ui_never_renders_server_text_as_html(client):
    js = client.get("/static/app.js").text
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_api_docs_keep_their_own_headers(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers


def test_api_routes_not_shadowed(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_chart_library_is_bundled_and_compressed(client):
    html = client.get("/").text
    assert 'src="/static/vendor/echarts/echarts.min.js"' in html
    response = client.get("/static/vendor/echarts/echarts.min.js", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert "content-security-policy" in response.headers
    assert client.get("/static/vendor/echarts/LICENSE").status_code == 200


def test_charts_never_render_tooltips_as_html(client):
    js = client.get("/static/app.js").text
    # ECharts tooltips default to HTML; richText draws them on the canvas instead.
    assert 'renderMode: "richText"' in js
