"""Native Google REST transport for the OneCLI-brokered Google surfaces.

Replaces `composio-rest.py` (nanoclaw#638). Ships in the trusted
`google-ops` skill (mounted on main + trusted, `jbaruch/nanoclaw-admin#456`)
as the single HTTP primitive for Gmail / Calendar / Tasks — shared by the
trusted-tier ground-truth verification path (`google-calendar.py`,
`google-tasks.py`, its siblings here) and the admin Google-facing skills
(check-email, morning-brief, manage-tasks, …) that load it off this mount.

Credential model (the whole point of #638)
------------------------------------------
This container holds NO Google credential. OneCLI's TLS-MITM gateway owns
the OAuth connection: it injects `Authorization: Bearer` on the wire to
the Google API hosts and auto-refreshes the token via its per-provider
`RefreshConfig`. Callers therefore send NO auth header and read no key
from the environment. `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` are gone —
a request carrying its own Authorization header would be a bug, not a
fallback.

The gateway reaches this process via `HTTPS_PROXY` + the mounted CA
bundle, both set on the spawn by the orchestrator. Nothing here
configures the proxy; `urllib` honours `HTTPS_PROXY` from the
environment.

Failure modes this maps to actionable errors
--------------------------------------------
Under Composio a missing key surfaced as an actionable `MissingCredentials`.
The native path has no key to miss, but it has two failure modes that a
bare HTTP status hides:

  401 -> `GatewayNotInjecting`. Either the OneCLI gateway is not on this
         process's request path (no HTTPS_PROXY / CA on the spawn, so the
         request went straight to Google unauthenticated), or the Google
         app is not connected in the vault. Both are config errors an
         operator must fix; neither is transient, so callers must not
         retry them as if they were.
  403 with `access_restricted` -> `TierAccessRestricted`. The agent's
         OneCLI `secretMode` is `selective` — the untrusted tier is
         deliberately gated this way (#638) and gets no Google reach. This
         is correct behaviour, not a fault; it is raised distinctly so an
         untrusted-tier caller can report "not available at this tier"
         rather than alarm about a broken gateway.

Every other non-2xx propagates as `urllib.error.HTTPError` for the caller
to wrap into its own per-call error marker, matching composio-rest.py's
contract.

Contract
--------
google_request(method, url, *, params=None, body=None,
               timeout=PER_CALL_TIMEOUT_SECONDS) -> dict
    Issue a native Google REST call and return the parsed JSON object.
    `params` is url-encoded onto the query string; `body` is JSON-encoded.
    A 204 (or any empty body) returns {} — Calendar's DELETE answers 204
    with no content, and callers should not have to special-case that.
    Raises GatewayNotInjecting / TierAccessRestricted / HTTPError /
    URLError / TimeoutError / OSError / json.JSONDecodeError.

Env overrides
-------------
GOOGLE_API_BASES — JSON object remapping the per-surface base URLs. Tests
    point the surfaces at a local fixture server; production uses the
    defaults below. Unset in production.

Reads only the non-secret account-selection file in addition to the HTTP call. This file is a function library, not a
script — there is no __main__ entry point.
"""

from __future__ import annotations

import io
import hashlib
from datetime import datetime, timezone
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID

# Per-surface API bases. These are the exact hosts OneCLI's app
# connections are bound to — changing a host here without adding the
# matching OneCLI app binding means the gateway won't inject a Bearer and
# the call 401s.
DEFAULT_API_BASES = {
    "gmail": "https://gmail.googleapis.com/gmail/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
    "tasks": "https://tasks.googleapis.com/tasks/v1",
    "drive": "https://www.googleapis.com/drive/v3",
}

# Per-call wall-clock budget — a real urlopen timeout, not an LLM
# judgement about wall-clock budget. Matches composio-rest.py's budget so
# the migration doesn't silently change per-call latency tolerance.
PER_CALL_TIMEOUT_SECONDS = 90.0

GATEWAY_NOT_INJECTING_HINT = (
    "the OneCLI gateway is not authenticating this request. Check that the spawn "
    "carries HTTPS_PROXY + the mounted CA (src/container-runner.ts), and that the "
    "Google app has a valid connection in OneCLI (`onecli apps list`). A listed connection may still have "
    "an expired or revoked refresh token and require Google reauthorization"
)

TIER_ACCESS_RESTRICTED_HINT = (
    "this agent's OneCLI secretMode is 'selective' — the untrusted tier is gated "
    "from Google by design (nanoclaw#638). Google-backed skills do not run at this tier"
)


class GatewayNotInjecting(RuntimeError):
    """Google rejected authentication, or the gateway has no usable connection.

    Distinct from a transient failure: the gateway is either off this
    process's request path or the app is disconnected. Retrying cannot
    fix either. Callers at a process boundary print
    GATEWAY_NOT_INJECTING_HINT to stderr and exit non-zero.
    """


class TierAccessRestricted(RuntimeError):
    """The OneCLI gateway refused to inject for this agent's tier.

    Expected on the untrusted tier (secretMode=selective). Callers report
    "unavailable at this tier" rather than treating it as a fault.
    """


def api_bases():
    """Resolve the per-surface base URLs, applying the GOOGLE_API_BASES
    test override. Read per-call rather than at import so a test can set
    the override after this module is loaded."""
    override = os.environ.get("GOOGLE_API_BASES")
    if not override:
        return dict(DEFAULT_API_BASES)
    bases = dict(DEFAULT_API_BASES)
    bases.update(json.loads(override))
    return bases


def surface_url(surface, path):
    """Join a surface's base with a path. `surface` is a DEFAULT_API_BASES
    key; unknown surfaces raise KeyError rather than silently building a
    URL the gateway has no app binding for."""
    bases = api_bases()
    if surface not in bases:
        raise KeyError(f"unknown Google surface {surface!r}; known: {sorted(bases)}")
    return f"{bases[surface]}/{path.lstrip('/')}"


def _query_value(value):
    """Render one param value for the query string.

    JSON booleans must reach Google as `true`/`false`. Left alone,
    urlencode stringifies Python's bool as `True`/`False`, which Google
    rejects as a malformed value — and the callers hand us booleans
    straight off stdin JSON (`singleEvents`, `showCompleted`,
    `showHidden`). Lists are mapped element-wise so doseq still sees a
    list.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_query_value(v) for v in value]
    return value


def connection_config():
    """Read non-secret routing metadata; credentials stay in OneCLI."""
    path = os.environ.get("GOOGLE_CONNECTIONS_FILE", "/workspace/global/google-connections.json")
    try:
        with open(path, encoding="utf-8") as stream:
            config = json.load(stream)
    except FileNotFoundError:
        return {"connections": {}}
    if not isinstance(config, dict) or not isinstance(config.get("connections"), dict):
        raise ValueError("Google connection selection config must contain a connections object")
    return config


def connection_headers(url, *, account=None):
    """Select one connection for the exact API origin/path, never by fallback."""
    target = urllib.parse.urlsplit(url)
    surface = None
    for name, base in DEFAULT_API_BASES.items():
        parsed = urllib.parse.urlsplit(base)
        if (target.scheme, target.netloc) == (parsed.scheme, parsed.netloc) and (
            target.path == parsed.path or target.path.startswith(parsed.path + "/")
        ):
            surface = name
            break
    if surface is None:
        return {}
    config = connection_config()
    if account is not None:
        accounts = config.get("calendar_accounts", {})
        if surface != "calendar" or not isinstance(account, str) or account not in accounts:
            raise ValueError(f"Unknown account for Google {surface}: {account!r}")
        connection_id = accounts[account]
    else:
        connection_id = config["connections"].get(surface)
    if connection_id is None:
        return {}
    if not isinstance(connection_id, str):
        raise ValueError(f"Invalid Google connection id for {surface}")
    try:
        UUID(connection_id)
    except ValueError as exc:
        raise ValueError(f"Invalid Google connection id for {surface}") from exc
    return {"x-onecli-connection-id": connection_id}


class CalendarReadError(RuntimeError):
    """An agenda is incomplete; callers must not treat it as an empty calendar."""


def _calendar_sources(config, account, calendar_id):
    sources = config.get("calendars")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Google calendars must be a non-empty list")
    accounts = config.get("calendar_accounts", {})
    for source in sources:
        if (not isinstance(source, dict) or source.get("account") not in accounts
                or not isinstance(source.get("calendarId"), str) or not source["calendarId"]):
            raise ValueError("Each calendar needs a configured account and calendarId")
    if account is not None:
        if not isinstance(account, str) or account not in accounts:
            raise ValueError(f"Unknown Google Calendar account: {account!r}")
        sources = [s for s in sources if s["account"] == account]
    if calendar_id is not None:
        if not isinstance(calendar_id, str):
            raise ValueError("calendarId must be a string")
        selected = [s for s in sources if calendar_id in (
            s["calendarId"], s["account"] if s["calendarId"] == "primary" else s["calendarId"])]
        if not selected and account is not None:
            selected = [{"account": account, "calendarId": calendar_id, "label": calendar_id}]
        sources = selected
    if not sources:
        raise ValueError("Calendar is not configured; specify its account and calendarId explicitly")
    # A shared calendar may be visible through both logins; fetch it only once.
    result, seen = [], set()
    for source in sources:
        actual_id = source["account"] if source["calendarId"] == "primary" else source["calendarId"]
        if actual_id not in seen:
            result.append({**source, "calendarId": actual_id})
            seen.add(actual_id)
    return result


def _event_instant(value):
    if not isinstance(value, dict):
        return ""
    stamp = value.get("dateTime")
    if stamp:
        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if instant.tzinfo is not None:
            return instant.astimezone(timezone.utc).isoformat()
        return stamp
    return value.get("date", "")


def _calendar_event_key(event, source):
    # UID + occurrence joins invitation copies without joining recurrence instances.
    occurrence = _event_instant(event.get("originalStartTime") or (
        event.get("start") if event.get("recurringEventId") else None))
    if event.get("iCalUID"):
        return [event["iCalUID"], occurrence]
    return [source["account"], source["calendarId"], event.get("id"), occurrence]


def calendar_events(params=None, *, account=None, calendar_id=None,
                    timeout=PER_CALL_TIMEOUT_SECONDS):
    """Read the configured agenda with account labels, paging and deduplication.

    A failure in any source fails the whole read, protecting reminder sweeps from
    interpreting an inaccessible calendar as deleted events. `id` is a stable,
    source-aware agenda key; native Google IDs are retained in calendarSources.
    With no multi-calendar config, preserve the legacy single-calendar response.
    """
    config = connection_config()
    params = dict(params or {})
    if account == "all":
        account = None
    if "calendars" not in config:
        path = "calendars/" + urllib.parse.quote(calendar_id or "primary", safe="") + "/events"
        kwargs = {"account": account} if account is not None else {}
        return google_request("GET", surface_url("calendar", path), params=params,
                              timeout=timeout, **kwargs)
    sources = _calendar_sources(config, account, calendar_id)
    if "pageToken" in params or "syncToken" in params:
        raise ValueError("Combined calendar reads manage pagination; pageToken/syncToken are unsupported")
    events = {}
    for source in sources:
        path = "calendars/" + urllib.parse.quote(source["calendarId"], safe="") + "/events"
        query = {"maxResults": 2500, **params}
        seen_tokens = set()
        try:
            for _ in range(100):
                resource = google_request("GET", surface_url("calendar", path), params=query,
                                          account=source["account"], timeout=timeout)
                if not isinstance(resource, dict) or not isinstance(resource.get("items", []), list):
                    raise ValueError("Malformed Calendar response")
                for event in resource.get("items", []):
                    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                        raise ValueError("Calendar event is missing its id")
                    key = "nc_" + hashlib.sha256(json.dumps(
                        _calendar_event_key(event, source), ensure_ascii=False
                    ).encode()).hexdigest()[:32]
                    provenance = {**source, "eventId": event["id"]}
                    previous = events.get(key)
                    if previous is None:
                        events[key] = {**event, "id": key, "calendarSources": [provenance]}
                    else:
                        if provenance not in previous["calendarSources"]:
                            previous["calendarSources"].append(provenance)
                        # An invitation accepted on one account still belongs on the agenda.
                        def declined(item):
                            return any(a.get("self") and a.get("responseStatus") == "declined"
                                       for a in item.get("attendees", []))
                        if declined(previous) and not declined(event):
                            events[key] = {**event, "id": key,
                                           "calendarSources": previous["calendarSources"]}
                token = resource.get("nextPageToken")
                if not token:
                    break
                if token in seen_tokens:
                    raise ValueError("Calendar pagination repeated a page token")
                seen_tokens.add(token)
                query["pageToken"] = token
            else:
                raise ValueError("Calendar pagination exceeded 100 pages; narrow the time window")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                GatewayNotInjecting, TierAccessRestricted) as exc:
            raise CalendarReadError(
                f"Calendar read failed for {source['account']} / {source.get('label', source['calendarId'])}: {exc}"
            ) from exc
    return {"kind": "calendar#events", "items": sorted(events.values(), key=lambda e: (
        _event_instant(e.get("start")), e["id"])), "sources": sources}


def google_request(method, url, *, params=None, body=None, timeout=PER_CALL_TIMEOUT_SECONDS, account=None):
    if params:
        # Google's list endpoints take repeated keys for multi-value params
        # (labelIds, fields); doseq keeps a list arg as repeats rather than
        # url-encoding the Python list repr.
        query = urllib.parse.urlencode(
            {k: _query_value(v) for k, v in params.items()}, doseq=True
        )
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    data = None
    headers = {"accept": "application/json", **connection_headers(url, account=account)}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"

    # No Authorization header by design — the OneCLI gateway injects it on
    # the wire. Setting one here would either be overwritten or shadow the
    # injection; either way it means a credential leaked into the container.
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise _classify(e) from e

    if not raw:
        # 204 No Content — Calendar/Tasks DELETE. An empty body is success,
        # not a JSONDecodeError for every caller to guard.
        return {}
    return json.loads(raw.decode("utf-8"))


def _classify(e):
    """Map an HTTPError to the actionable error for its config failure
    mode, or return an equivalent HTTPError for the caller to wrap.

    Reads the body once — it is the only place the `access_restricted`
    marker appears. Reading it is destructive, so an ordinary 403 cannot
    simply be handed back: its body would already be drained, and the
    caller would wrap a bare "HTTP Error 403: Forbidden" having lost the
    reason Google actually sent (`rateLimitExceeded`,
    `insufficientPermissions`, `dailyLimitExceeded`). It is rebuilt with
    the detail in `reason` and the body restored, so `str(e)` carries the
    diagnostic and `.read()` still works.
    """
    if e.code not in (401, 403):
        return e
    try:
        raw = e.read()
    except OSError:
        raw = b""
    detail = raw.decode("utf-8", "replace")
    if e.code == 401:
        return GatewayNotInjecting(f"{GATEWAY_NOT_INJECTING_HINT} (Google said: {detail[:300]})")
    if "access_restricted" in detail:
        return TierAccessRestricted(TIER_ACCESS_RESTRICTED_HINT)
    reason = f"{e.reason} ({detail[:300]})" if detail else e.reason
    return urllib.error.HTTPError(e.url, e.code, reason, e.headers, io.BytesIO(raw))
