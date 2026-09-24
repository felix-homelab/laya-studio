"""The page shells, and the one contract that keeps them working.

A page is a server-rendered shell plus per-page scripts, and the two have to come
from the same revision of the tree. Starlette serves ``/static`` with
``ETag``/``Last-Modified`` but no ``Cache-Control``, which leaves a browser free
to reuse a script *without revalidating*; a reload then pairs a new shell with an
old script and the page throws before it finishes wiring itself up. Stamping each
asset URL with its own mtime and size
(:func:`app.templating.static_url`) is what prevents that.
"""

from __future__ import annotations

import re

PAGES = ("/", "/schemas", "/bulk", "/ab", "/workflows", "/analytics", "/review")

ASSET_ATTRIBUTE = re.compile(r'(?:src|href)="(/static/[^"]+)"')


def test_every_page_renders(client):
    for path in PAGES:
        assert client.get(path).status_code == 200, path


def test_every_page_loads_only_version_stamped_assets(client):
    for path in PAGES:
        assets = ASSET_ATTRIBUTE.findall(client.get(path).text)
        assert assets, path
        for url in assets:
            assert "?v=" in url, f"{path} would let a browser serve a stale copy of {url}"


def test_the_stamp_follows_the_file(tmp_path, monkeypatch):
    from app import templating

    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    (tmp_path / "js").mkdir()
    asset = tmp_path / "js" / "app.js"

    asset.write_text("one")
    first = templating.static_url("js/app.js")
    assert first.startswith("/static/js/app.js?v=")

    # Same second, different contents: the size has to carry the change.
    asset.write_text("one, but longer")
    assert templating.static_url("js/app.js") != first


def test_a_missing_asset_still_resolves_to_a_url(tmp_path, monkeypatch):
    from app import templating

    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    assert templating.static_url("/app/static/css/typo.css") == "/static/css/typo.css?v=0"
