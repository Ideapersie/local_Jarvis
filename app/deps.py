"""Request-scoped dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app import config
from app.db import engine

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


def asset_version() -> int:
    """Stylesheet mtime, used as a cache-busting query on every app.css link.

    StaticFiles sends ETag and Last-Modified but no Cache-Control, which leaves
    browsers free to heuristically cache app.css and never revalidate. Changing
    the URL is the only thing that reliably forces a refetch, and a stale
    stylesheet renders new markup unstyled with no error anywhere to explain it.
    """
    try:
        return int(config.STATIC_DIR.joinpath("app.css").stat().st_mtime)
    except OSError:
        return 0


# A template global rather than per-route context: every page links the same
# stylesheet, and a route that forgot to pass it would reintroduce the bug.
templates.env.globals["asset_version"] = asset_version


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
