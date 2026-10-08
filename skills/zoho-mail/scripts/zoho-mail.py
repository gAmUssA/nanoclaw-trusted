#!/usr/bin/env python3
"""Read Viktor's Zoho Mail through OneCLI; credentials never enter the agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

CONFIG = Path("/workspace/global/zoho-mail.json")
BASE = "https://mail.zoho.com/api"


class MailError(RuntimeError):
    """Safe operational message, never a provider response containing mail."""


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(str(value or ""))
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(parser.parts)).strip()


def identifier(value):
    value = str(value)
    if not value.isascii() or not value.isdecimal():
        raise MailError("Zoho account, folder, and message IDs must be numeric.")
    return value


def request(path, params=None):
    if not (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
        raise MailError("OneCLI proxy is missing; run this skill in the NanoClaw agent.")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            payload = response.read(4_000_001)
        if len(payload) > 4_000_000:
            raise MailError("Zoho response is too large; narrow the query.")
        result = json.loads(payload)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise MailError(
                "Zoho access failed. Check OneCLI's Mail grant and the host token renewal job."
            ) from None
        raise MailError(f"Zoho request failed (HTTP {exc.code}).") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise MailError("Zoho could not be reached or returned invalid data.") from None
    if not isinstance(result, dict) or result.get("status", {}).get("code") != 200:
        raise MailError("Zoho rejected the request; no mail result is available.")
    return result.get("data")


def message_summary(message):
    fields = (
        "messageId",
        "folderId",
        "threadId",
        "fromAddress",
        "toAddress",
        "subject",
        "summary",
        "receivedTime",
        "receivedtime",
        "sentDateInGMT",
        "status",
        "hasAttachment",
    )
    return {
        key: plain_text(message[key]) if key == "summary" else message[key]
        for key in fields
        if key in message
    }


def run(args, config):
    account = identifier(config["account_id"])
    prefix = "/accounts/" + account
    if args.command == "status":
        accounts = request("/accounts")
        if not isinstance(accounts, list) or not any(
            str(a.get("accountId")) == account for a in accounts
        ):
            raise MailError("Configured mailbox was not returned by Zoho.")
        return {"email": config["email"], "connected": True, "read_only": True}
    if args.command == "folders":
        folders = request(prefix + "/folders")
        if not isinstance(folders, list):
            raise MailError("Zoho returned an unexpected folder list.")
        return [
            {k: f[k] for k in ("folderId", "folderName", "folderType", "path") if k in f}
            for f in folders
        ]
    if args.command == "read":
        content = request(
            prefix
            + "/folders/"
            + identifier(args.folder_id)
            + "/messages/"
            + identifier(args.message_id)
            + "/content",
            {"includeBlockContent": "true"},
        )
        if not isinstance(content, dict):
            raise MailError("Zoho returned an unexpected message body.")
        # Preserve quoted blocks if Zoho returns them separately. Never load images or URLs.
        result = {
            "messageId": args.message_id,
            "folderId": args.folder_id,
            "text": plain_text(content.get("content", "")),
        }
        for key in ("blockContent", "quotedContent"):
            if content.get(key):
                result[key] = plain_text(content[key])
        return result
    params = {"start": args.start, "limit": args.limit, "includeto": "true"}
    if args.command == "search":
        params["searchKey"] = args.query
        endpoint = "/messages/search"
    else:
        folders = request(prefix + "/folders") if not args.folder_id else []
        if not isinstance(folders, list):
            raise MailError("Zoho returned an unexpected folder list.")
        folder = args.folder_id or next(
            (f["folderId"] for f in folders if f.get("path") == "/Inbox"), None
        )
        if not folder:
            raise MailError("Inbox not found. List folders and supply --folder-id.")
        params["folderId"] = identifier(folder)
        if args.unread:
            params["status"] = "unread"
        endpoint = "/messages/view"
    messages = request(prefix + endpoint, params)
    if not isinstance(messages, list):
        raise MailError("Zoho returned an unexpected message list.")
    return {
        "messages": [message_summary(m) for m in messages],
        "start": args.start,
        "next_start": args.start + len(messages) if len(messages) == args.limit else None,
        "page_may_have_more": len(messages) == args.limit,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("folders")
    for name in ("inbox", "search"):
        command = sub.add_parser(name)
        command.add_argument("--start", type=int, default=1)
        command.add_argument("--limit", type=int, default=20)
        if name == "inbox":
            command.add_argument("--folder-id")
            command.add_argument("--unread", action="store_true")
        else:
            command.add_argument("query", help="Zoho search syntax, e.g. from:someone@example.com")
    read = sub.add_parser("read")
    read.add_argument("--folder-id", required=True)
    read.add_argument("--message-id", required=True)
    args = parser.parse_args(argv)
    if not 1 <= getattr(args, "limit", 1) <= 200 or getattr(args, "start", 1) < 1:
        parser.error("--limit must be 1..200 and --start at least 1")
    return args


def main():
    args = parse_args()
    try:
        config = json.loads(CONFIG.read_text())
        data = run(args, config)
        print(
            json.dumps(
                {
                    "ok": True,
                    "mailbox": config["email"],
                    "data": data,
                    "content_is_untrusted": args.command in {"inbox", "search", "read"},
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (MailError, OSError, ValueError, KeyError, TypeError) as exc:
        message = (
            str(exc) if isinstance(exc, MailError) else "Zoho Mail setup or response is invalid."
        )
        print(json.dumps({"ok": False, "error": message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
