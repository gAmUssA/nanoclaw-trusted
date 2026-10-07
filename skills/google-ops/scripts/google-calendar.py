#!/usr/bin/env python3
"""Read Google's configured calendars through OneCLI, without credentials.

Usage: google-calendar.py events-list (JSON arguments on stdin).
Native query arguments include timeMin, timeMax, singleEvents and orderBy.
singleEvents defaults to true. With /workspace/global/google-connections.json,
no selector reads every configured calendar: both Viktor accounts plus Family.
Optional account selects a login; calendarId selects a calendar. A primary
calendar email selects its matching login automatically. Unknown calendars
require an explicit account. Without multi-calendar configuration the original
single primary-calendar behavior is preserved.

Output: {"kind":"calendar#events", "items":[...], "sources":[...]}.
The helper follows every page and combines shared invitation copies by iCalUID
and recurring occurrence. Each event retains native fields except id, which is
an opaque stable agenda key for reminder/state comparisons. calendarSources
retains each native eventId, account, calendarId and label. Never use the agenda
id as a Google API mutation id. Reading any source unsuccessfully returns a
nonzero exit and no stdout; never interpret that as no events.

This script exposes reads only. Google OAuth tokens stay in OneCLI.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import urllib.error

_SCRIPTS = pathlib.Path(__file__).resolve().parent
# google-rest.py is this script's sibling (the shared Google REST
# primitives live in this skill's scripts/ dir). Loaded by file path because the
# hyphenated filename is not a valid import name.
GOOGLE_REST_PATH = _SCRIPTS / "google-rest.py"

# singleEvents=true expands recurring events into individual instances.
# Every caller reads a concrete day's agenda, so the expanded form is what
# they mean; the unexpanded form would hand them a recurrence rule to
# interpret. orderBy=startTime is only legal alongside it.
DEFAULT_QUERY = {"singleEvents": "true"}


def _load_google_rest():
    spec = importlib.util.spec_from_file_location("google_rest", GOOGLE_REST_PATH)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load google-rest from {GOOGLE_REST_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_stdin_args() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    args = json.loads(raw)
    if not isinstance(args, dict):
        raise ValueError("stdin must be a JSON object of Calendar arguments")
    return args


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "events-list":
        sys.stderr.write("google-calendar: usage: google-calendar.py events-list (args on stdin)\n")
        return 2

    try:
        overrides = _read_stdin_args()
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(f"google-calendar: invalid stdin ({e}).\n")
        return 2

    try:
        google_rest = _load_google_rest()
    except (FileNotFoundError, PermissionError, ImportError, OSError) as e:
        sys.stderr.write(
            f"google-calendar: Google REST helper unavailable ({e}) — "
            f"expected at {GOOGLE_REST_PATH}.\n"
        )
        return 2

    # Drop empty/None overrides so a blank `calendarId` can't shadow the
    # "primary" default and produce an opaque Calendar 4xx.
    args = {k: v for k, v in overrides.items() if v not in (None, "")}
    calendar_id = args.pop("calendarId", None)
    account = args.pop("account", None)
    # Booleans arriving from stdin JSON (`singleEvents`) are serialized by
    # google_request, not here — one encoder, so every op script agrees.
    params = {**DEFAULT_QUERY, **args}

    try:
        resource = google_rest.calendar_events(params, account=account, calendar_id=calendar_id)
    except google_rest.GatewayNotInjecting as e:
        sys.stderr.write(f"google-calendar: events-list unauthenticated — {e}\n")
        return 1
    except google_rest.TierAccessRestricted as e:
        sys.stderr.write(f"google-calendar: events-list unavailable at this tier — {e}\n")
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, google_rest.CalendarReadError) as e:
        sys.stderr.write(f"google-calendar: events-list call failed ({type(e).__name__}: {e}).\n")
        return 1

    print(json.dumps(resource))
    return 0


if __name__ == "__main__":
    sys.exit(main())
