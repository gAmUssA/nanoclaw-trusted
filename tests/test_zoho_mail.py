"""Read-only Zoho transport: fail closed, page honestly, and never supply tokens."""

import importlib.util
import json
from io import BytesIO
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/zoho-mail/scripts/zoho-mail.py"
spec = importlib.util.spec_from_file_location("zoho_mail", SCRIPT)
assert spec is not None and spec.loader is not None
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)
CONFIG = {"account_id": "123", "email": "owner@example.com"}


def test_mail_body_is_text_and_does_not_include_executable_html():
    assert mail.plain_text(
        "<p>Hello &amp; welcome</p><script>steal()</script><style>hide</style>"
    ) == ("Hello & welcome")


@pytest.mark.parametrize("value", ["../accounts", "123?secret=x", "１２３", "12/3"])
def test_ids_cannot_inject_another_endpoint(value):
    with pytest.raises(mail.MailError):
        mail.identifier(value)


def test_missing_proxy_does_not_attempt_direct_access(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(
        mail.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("direct access")
    )
    with pytest.raises(mail.MailError, match="proxy is missing"):
        mail.request("/accounts")


def test_request_is_get_without_auth_and_provider_errors_are_not_empty_inbox(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    seen = []

    def fake_open(request, **kwargs):
        seen.append(request)
        return BytesIO(json.dumps({"status": {"code": 400}, "data": []}).encode())

    monkeypatch.setattr(mail.urllib.request, "urlopen", fake_open)
    with pytest.raises(mail.MailError, match="rejected"):
        mail.request("/accounts/123/messages/search", {"searchKey": "from:a+b@example.com"})
    assert seen[0].get_method() == "GET"
    assert not seen[0].has_header("Authorization")
    assert "searchKey=from%3Aa%2Bb%40example.com" in seen[0].full_url


def test_full_page_reports_possible_more_and_search_keeps_query(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return [{"messageId": "456", "folderId": "789", "subject": "hello"}]

    monkeypatch.setattr(mail, "request", fake_request)
    result = mail.run(
        mail.parse_args(["search", "from:someone@example.com", "--limit", "1"]), CONFIG
    )
    assert result["page_may_have_more"] is True
    assert result["next_start"] == 2
    assert calls == [
        (
            "/accounts/123/messages/search",
            {
                "start": 1,
                "limit": 1,
                "includeto": "true",
                "searchKey": "from:someone@example.com",
            },
        )
    ]


def test_inbox_uses_inbox_folder_not_all_mail(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        if path.endswith("/folders"):
            return [{"folderId": "789", "path": "/Inbox"}]
        return []

    monkeypatch.setattr(mail, "request", fake_request)
    result = mail.run(mail.parse_args(["inbox", "--unread"]), CONFIG)
    assert calls[1][1]["folderId"] == "789"
    assert calls[1][1]["status"] == "unread"
    assert result["messages"] == []
    assert result["next_start"] is None


def test_read_preserves_quoted_content_and_only_uses_content_endpoint(monkeypatch):
    calls = []

    def fake_request(path, params=None):
        calls.append((path, params))
        return {"content": "<p>Answer</p>", "blockContent": "<blockquote>Question</blockquote>"}

    monkeypatch.setattr(mail, "request", fake_request)
    result = mail.run(
        mail.parse_args(["read", "--folder-id", "789", "--message-id", "456"]), CONFIG
    )
    assert result["text"] == "Answer"
    assert result["blockContent"] == "Question"
    assert calls == [
        ("/accounts/123/folders/789/messages/456/content", {"includeBlockContent": "true"})
    ]
