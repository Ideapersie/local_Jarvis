"""Stylesheet cache busting.

StaticFiles sends ETag and Last-Modified but no Cache-Control, so browsers are
free to serve app.css from cache without revalidating. That is not theoretical:
a header component shipped once and rendered unstyled because the browser held
the previous stylesheet. The version query makes the URL change with the file.
"""

from __future__ import annotations

import re

from app import config, deps

LINK = re.compile(r'href="/static/app\.css\?v=(\d+)"')


def expected_version() -> int:
    return int(config.STATIC_DIR.joinpath("app.css").stat().st_mtime)


def test_asset_version_is_the_stylesheet_mtime():
    assert deps.asset_version() == expected_version()


def test_dashboard_links_the_versioned_stylesheet(client):
    found = LINK.search(client.get("/").text)
    assert found, "dashboard did not link a versioned app.css"
    assert int(found.group(1)) == expected_version()


def test_career_page_links_the_versioned_stylesheet(client):
    found = LINK.search(client.get("/career").text)
    assert found, "career page did not link a versioned app.css"


def test_no_template_links_the_unversioned_stylesheet():
    # A template that misses the version silently keeps the stale-cache bug for
    # whichever page it serves.
    for page in sorted(config.TEMPLATES_DIR.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        if "app.css" not in text:
            continue
        assert 'href="/static/app.css"' not in text, page.name
