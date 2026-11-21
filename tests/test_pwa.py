"""Installable app (PWA): manifest, icons, service worker, offline page."""

import struct
from pathlib import Path

from poghiamo.webapp import app as app_module

STATIC = Path(app_module.__file__).parent / "static"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


def test_manifest_is_public_and_points_at_real_icons(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    manifest = resp.json()
    assert (manifest["start_url"], manifest["scope"], manifest["display"]) == ("/", "/", "standalone")

    purposes = set()
    for icon in manifest["icons"]:
        width, height = (int(n) for n in icon["sizes"].split("x"))
        path = STATIC / icon["src"].removeprefix("/static/")
        assert _png_size(path) == (width, height), icon["src"]
        assert client.get(icon["src"]).status_code == 200
        purposes.add(icon.get("purpose", "any"))
    assert purposes == {"any", "maskable"}  # Android needs both kinds


def test_apple_touch_icon_is_180px():
    assert _png_size(STATIC / "icons" / "apple-touch-icon.png") == (180, 180)


def test_service_worker_is_served_from_the_root_and_never_cached(client):
    resp = client.get("/sw.js")  # no login: the login page registers it too
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    assert resp.headers["cache-control"] == "no-cache"
    body = resp.text
    # No notifications in this app, by decision.
    assert '"push"' not in body and "showNotification" not in body
    for url in ("/offline", "/static/tailwind.css", "/static/icons/icon-192.png"):
        assert f'"{url}"' in body  # precached for offline use


def test_offline_page_is_public(client):
    resp = client.get("/offline")
    assert resp.status_code == 200
    assert "Sei offline" in resp.text


def test_pages_link_the_manifest_and_register_the_worker(client):
    page = client.get("/login").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert 'href="/static/icons/apple-touch-icon.png"' in page
    assert 'navigator.serviceWorker.register("/sw.js")' in page
    assert 'id="offline-banner"' in page
    # Logging in on a shared device must not show the previous account's pages.
    assert 'caches.delete("poghiamo-pages")' in page
