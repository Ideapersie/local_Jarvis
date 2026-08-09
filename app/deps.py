"""Request-scoped dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app import config
from app.db import engine

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
