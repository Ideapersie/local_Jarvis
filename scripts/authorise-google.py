"""One-time Google consent, run by hand.

The first authorisation opens a browser. That must not happen from inside a
06:25 scheduled job - it would hang waiting for a click nobody is there to make.
Run this once after dropping credentials.json in the repo root; every run after
that refreshes the cached token silently.

    .venv/Scripts/python.exe scripts/authorise-google.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from app.integrations import gcal, gmail, google_auth

    if not google_auth.configured():
        print(f"No {google_auth.CREDENTIALS_PATH.name} in the repo root.")
        print("See the Google Cloud section of README.md.")
        return 1

    print("Requesting scopes:")
    for scope in google_auth.SCOPES:
        print(f"  {scope}")
    print("\nA browser window will open. Approve, then come back here.\n")

    try:
        google_auth.service("gmail", "v1")
    except Exception as exc:
        print(f"Authorisation failed: {exc}")
        return 1

    print(f"Token cached to {google_auth.token_path().name}.\n")

    messages = gmail.recent(hours=24)
    print(f"Gmail   : {len(messages)} messages in the last 24h")
    for m in messages[:3]:
        print(f"          {m.sender}: {m.subject[:60]}")

    events = gcal.upcoming(days=7)
    print(f"Calendar: {len(events)} events in the next 7 days")
    for e in events[:3]:
        print(f"          {e.day} {e}")

    print("\nBoth reachable. Triage runs at 06:25, the brief at 06:30.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
