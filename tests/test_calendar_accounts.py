"""Multi-account agenda contract: routing, completeness and shared invitations."""

import importlib.util
import io
import json
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REST_PATH = ROOT / "skills/google-ops/scripts/google-rest.py"
PERSONAL = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


def load_rest():
    spec = importlib.util.spec_from_file_location("calendar_accounts_test", REST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def calendars(tmp_path, monkeypatch):
    config = {
        "connections": {"calendar": PERSONAL},
        "calendar_accounts": {"personal@example.com": PERSONAL, "second@example.com": SECOND},
        "calendars": [
            {"account": "personal@example.com", "calendarId": "primary", "label": "Personal"},
            {"account": "second@example.com", "calendarId": "primary", "label": "Second"},
            {"account": "personal@example.com", "calendarId": "family@group", "label": "Family"},
        ],
    }
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("GOOGLE_CONNECTIONS_FILE", str(path))
    monkeypatch.delenv("GOOGLE_API_BASES", raising=False)
    return path, config


@pytest.fixture
def api(monkeypatch):
    requests = []
    responses = {}

    def urlopen(request, **kwargs):
        headers = {k.lower(): v for k, v in request.header_items()}
        assert "authorization" not in headers
        url = urllib.parse.urlsplit(request.full_url)
        calendar = urllib.parse.unquote(url.path.split("/")[-2])
        query = urllib.parse.parse_qs(url.query)
        assert "account" not in query and "calendarId" not in query
        requests.append((calendar, headers.get("x-onecli-connection-id"), query, kwargs))
        page = query.get("pageToken", [None])[0]
        response = responses.get((calendar, page), responses.get(calendar, {"items": []}))
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requests, responses


def event(identifier="event", uid="shared", start="2026-10-08T10:00:00-04:00", **kw):
    return {"id": identifier, "iCalUID": uid, "start": {"dateTime": start}, **kw}


def test_all_three_calendars_route_through_their_own_account(calendars, api):
    requests, responses = api
    responses["personal@example.com"] = {"items": [event(uid="personal")]}
    responses["second@example.com"] = {"items": [event(uid="second")]}
    responses["family@group"] = {"items": [event(uid="family")]}
    result = load_rest().calendar_events({"singleEvents": True}, timeout=7)
    assert [(r[0], r[1]) for r in requests] == [
        ("personal@example.com", PERSONAL),
        ("second@example.com", SECOND),
        ("family@group", PERSONAL),
    ]
    assert len(result["items"]) == 3
    assert len({e["id"] for e in result["items"]}) == 3  # Native IDs can collide.
    assert {s["label"] for s in result["sources"]} == {"Personal", "Second", "Family"}
    assert all(r[3]["timeout"] == 7 for r in requests)


def test_shared_invitation_is_one_event_with_both_sources(calendars, api):
    _, responses = api
    responses["personal@example.com"] = {"items": [event(identifier="a")]}
    responses["second@example.com"] = {
        "items": [event(identifier="b", start="2026-10-08T14:00:00Z")]
    }
    result = load_rest().calendar_events()
    assert len(result["items"]) == 1
    assert {s["eventId"] for s in result["items"][0]["calendarSources"]} == {"a", "b"}


def test_recurring_instances_stay_distinct_and_id_survives_reschedule(calendars, api):
    _, responses = api
    first = event(originalStartTime={"dateTime": "2026-10-08T14:00:00Z"})
    second = event(originalStartTime={"dateTime": "2026-10-09T14:00:00Z"})
    responses["personal@example.com"] = {"items": [first, second]}
    rest = load_rest()
    result = rest.calendar_events()
    assert len(result["items"]) == 2
    before = {e["id"] for e in result["items"]}
    first["start"] = {"dateTime": "2026-10-10T14:00:00Z"}
    assert {e["id"] for e in rest.calendar_events()["items"]} == before


def test_one_off_event_id_survives_reschedule(calendars, api):
    _, responses = api
    item = event()
    responses["personal@example.com"] = {"items": [item]}
    rest = load_rest()
    before = rest.calendar_events()["items"][0]["id"]
    item["start"] = {"dateTime": "2026-10-09T14:00:00Z"}
    assert rest.calendar_events()["items"][0]["id"] == before


def test_all_pages_are_read_even_after_empty_page(calendars, api):
    requests, responses = api
    responses["personal@example.com"] = {"items": [], "nextPageToken": "next"}
    responses[("personal@example.com", "next")] = {"items": [event()]}
    result = load_rest().calendar_events()
    assert len(result["items"]) == 1
    assert requests[1][2]["pageToken"] == ["next"]


def test_repeated_page_token_fails_instead_of_infinite_loop(calendars, api):
    _, responses = api
    responses["personal@example.com"] = {"items": [], "nextPageToken": "same"}
    rest = load_rest()
    with pytest.raises(rest.CalendarReadError, match="repeated a page token"):
        rest.calendar_events()


def test_failed_second_account_never_returns_partial_agenda(calendars, api):
    _, responses = api
    responses["personal@example.com"] = {"items": [event()]}
    responses["second@example.com"] = urllib.error.HTTPError(
        "https://www.googleapis.com/calendar/v3/",
        401,
        "Unauthorized",
        Message(),
        io.BytesIO(b'{"error":{"message":"Expired"}}'),
    )
    rest = load_rest()
    with pytest.raises(rest.CalendarReadError, match="second@example.com"):
        rest.calendar_events()


@pytest.mark.parametrize(
    "selector", [{"account": "second@example.com"}, {"calendar_id": "second@example.com"}]
)
def test_account_and_email_calendar_selectors_choose_second_connection(calendars, api, selector):
    requests, _ = api
    load_rest().calendar_events(**selector)
    assert [(r[0], r[1]) for r in requests] == [("second@example.com", SECOND)]


def test_unknown_account_never_falls_back_to_personal(calendars, api):
    requests, _ = api
    with pytest.raises(ValueError, match="Unknown Google Calendar account"):
        load_rest().calendar_events(account="typo@example.com")
    assert requests == []


def test_unknown_calendar_requires_explicit_account(calendars, api):
    requests, _ = api
    rest = load_rest()
    with pytest.raises(ValueError, match="specify its account"):
        rest.calendar_events(calendar_id="unknown@group")
    assert requests == []
    rest.calendar_events(account="second@example.com", calendar_id="unknown@group")
    assert requests[0][0:2] == ("unknown@group", SECOND)


def test_shared_calendar_visible_to_two_accounts_is_fetched_once(calendars, api):
    path, config = calendars
    config["calendars"].append(
        {"account": "second@example.com", "calendarId": "family@group", "label": "Family"}
    )
    path.write_text(json.dumps(config))
    requests, _ = api
    load_rest().calendar_events()
    assert len(requests) == 3


def test_accepted_copy_wins_over_declined_copy(calendars, api):
    _, responses = api
    responses["personal@example.com"] = {
        "items": [event(attendees=[{"self": True, "responseStatus": "declined"}])]
    }
    responses["second@example.com"] = {
        "items": [event(attendees=[{"self": True, "responseStatus": "accepted"}])]
    }
    item = load_rest().calendar_events()["items"][0]
    assert item["attendees"][0]["responseStatus"] == "accepted"
    assert len(item["calendarSources"]) == 2


def test_direct_connection_selection_rejects_wrong_surface(calendars):
    rest = load_rest()
    with pytest.raises(ValueError, match="Unknown account for Google gmail"):
        rest.connection_headers(
            rest.surface_url("gmail", "users/me/profile"), account="second@example.com"
        )


def test_calendar_cli_defaults_to_complete_agenda(calendars, api, monkeypatch, capsys):
    requests, _ = api
    spec = importlib.util.spec_from_file_location(
        "calendar_cli_test", ROOT / "skills/google-ops/scripts/google-calendar.py"
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr("sys.argv", ["google-calendar.py", "events-list"])
    monkeypatch.setattr("sys.stdin", io.StringIO('{"timeMin":"2026-10-08T00:00:00Z"}'))
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["sources"]) == 3
    assert len(requests) == 3


def test_calendar_cli_reports_failed_account_without_partial_stdout(
    calendars, api, monkeypatch, capsys
):
    _, responses = api
    responses["second@example.com"] = urllib.error.URLError("connection down")
    spec = importlib.util.spec_from_file_location(
        "calendar_cli_test", ROOT / "skills/google-ops/scripts/google-calendar.py"
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr("sys.argv", ["google-calendar.py", "events-list"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main() == 1
    output = capsys.readouterr()
    assert not output.out
    assert "second@example.com" in output.err
