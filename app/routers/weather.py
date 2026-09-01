"""Header weather component.

Its own lazy-loaded route rather than part of the index context. fetch() waits
up to 15 seconds and the API drops connections often, so building this into the
synchronous index route would hang the whole dashboard on a bad handshake.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.deps import templates
from app.integrations import weather as weather_mod

router = APIRouter(prefix="/weather", tags=["weather"])


def build_context() -> dict:
    """View model for the header strip. Never raises, never fabricates."""
    fc = weather_mod.fetch_cached()
    return {
        "location": fc.location if fc else None,
        # Anchors carry their own None slots, so a partial forecast renders as
        # a gap in the right column rather than a shifted row.
        "anchors": (
            list(zip(weather_mod.ANCHOR_HOURS, fc.anchors(), strict=True))
            if fc
            else []
        ),
        "label": weather_mod.hour_label,
    }


@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request):
    return templates.TemplateResponse(
        request, "partials/weather_head.html", build_context()
    )
