"""Localhost hardening.

A server on 127.0.0.1 is not private. Any page you have open in the same browser
can issue a no-cors POST to it, and a hostile DNS record can point a domain at
127.0.0.1 to get same-origin access. Neither is exotic; both are cheap to close.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

# Host header values the app will answer to. Add a tailnet name here if the
# dashboard is ever exposed over Tailscale.
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "localhost:8000",
    "127.0.0.1:8000",
    "testserver",  # starlette TestClient
]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def block_cross_site(request: Request) -> None:
    """Reject state-changing requests that a browser marks as cross-site.

    `Sec-Fetch-Site` is set by the browser and is not reachable from JS, so a
    hostile page cannot spoof it. Requests without the header - curl, the test
    client, older browsers - are allowed through: this is defence against
    drive-by requests from a page you happen to have open, not an auth layer.
    """
    if request.method in SAFE_METHODS:
        return
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site request blocked")
