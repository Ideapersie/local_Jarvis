"""Shared Google OAuth: desktop flow, cached token, read-only scopes.

One token covers Gmail and Calendar. Requesting both scopes together means a
single consent screen rather than two, and the cached token.json is refreshed
in place.

Read-only on purpose. Nothing here needs to change your mail or calendar, and a
read-only token cannot damage either if something goes wrong.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app import config

log = logging.getLogger("jarvis.google")

CREDENTIALS_PATH = config.ROOT / "credentials.json"
TOKEN_PATH = config.ROOT / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def configured() -> bool:
    """True if the OAuth client file exists. Not the same as authorised."""
    return CREDENTIALS_PATH.exists()


def authorised() -> bool:
    return TOKEN_PATH.exists()


def _load_credentials():
    """Return valid credentials, refreshing or prompting as needed.

    Raises rather than returning None so callers get a real reason. Each caller
    decides whether a missing Google setup is fatal - for the brief it is not.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as exc:
            # The usual cause is an OAuth consent screen left in "Testing",
            # which expires refresh tokens after seven days. Say so, because
            # the raw error does not.
            log.warning(
                "token refresh failed (%s). If this recurs weekly, the OAuth "
                "consent screen is still in Testing - publish it to production.",
                exc,
            )
            TOKEN_PATH.unlink(missing_ok=True)
            creds = None

    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"No {CREDENTIALS_PATH.name}. See the Google Cloud section of README.md."
        )

    # Opens a browser once. Only reachable when run interactively; a scheduled
    # job with no token will fail loudly rather than hang on a prompt.
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    log.info("google authorised, token cached to %s", TOKEN_PATH.name)
    return creds


def service(name: str, version: str):
    """Build a Google API client. `name` is 'gmail' or 'calendar'."""
    from googleapiclient.discovery import build

    return build(name, version, credentials=_load_credentials(), cache_discovery=False)


def token_path() -> Path:
    return TOKEN_PATH
