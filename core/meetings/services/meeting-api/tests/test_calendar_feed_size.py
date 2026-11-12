"""The ICS feed size cap — ``CALENDAR_SYNC_MAX_ICS_BYTES``.

A secret iCal address exports the whole calendar, past events included, so a work calendar with
years of meetings runs to megabytes while ``parse_ics`` keeps two weeks of it. The cap bounds one
fetch's memory: the body is streamed and the fetch stops at the cap, and a declared Content-Length
over it is refused on the head alone. An HTML page keeps its own message at any size.
"""
from __future__ import annotations

import gzip
from datetime import datetime, timezone

import httpx
import pytest

from meeting_api.calendar_sync import parse_ics
from meeting_api.calendar_sync.adapters import DEFAULT_MAX_ICS_BYTES, fetch_ics, max_ics_bytes

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
URL = "https://calendar.example.com/private-abc/basic.ics"
MB = 1024 * 1024


def _event(uid: str, start: str, location: str = "") -> str:
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20200101T000000Z\r\nDTSTART:{start}\r\n"
            f"SUMMARY:Meeting {uid}\r\nDESCRIPTION:{'agenda notes ' * 20}\r\n"
            + (f"LOCATION:{location}\r\n" if location else "") + "END:VEVENT\r\n")


def _feed(min_bytes: int) -> str:
    """A whole-history export: years of past meetings, then one upcoming Meet call."""
    head = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    upcoming = _event("upcoming", "20260708T150000Z", "https://meet.google.com/abc-defg-hij")
    tail = upcoming + "END:VCALENDAR\r\n"
    past, size, i = [], len(head) + len(tail), 0
    while size < min_bytes:
        ev = _event(f"past-{i}", f"20{18 + i % 7:02d}0{1 + i % 9}{10 + i % 18:02d}T090000Z")
        past.append(ev)
        size += len(ev)
        i += 1
    return head + "".join(past) + tail


class _Body:
    """A streamed response body that counts what the fetch actually pulled off the wire."""

    def __init__(self, data: bytes, chunk: int = 64 * 1024):
        self._chunks = [data[i:i + chunk] for i in range(0, len(data), chunk)]
        self.sent = 0

    async def __aiter__(self):
        for c in self._chunks:
            self.sent += len(c)
            yield c


def _client(body, headers=None) -> httpx.AsyncClient:
    def handler(request):
        content = body if isinstance(body, (bytes, _Body)) else body.encode()
        return httpx.Response(200, headers=headers or {}, content=content)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_whole_history_feed_over_the_old_2mb_cap_imports_its_upcoming_meeting(monkeypatch):
    monkeypatch.delenv("CALENDAR_SYNC_MAX_ICS_BYTES", raising=False)
    feed = _feed(3 * MB)
    assert len(feed.encode()) > 2 * MB
    async with _client(feed) as client:
        text, err = await fetch_ics(URL, client=client)
    assert err is None and text == feed
    events = parse_ics(text, now=NOW)["events"]
    assert [e["uid"] for e in events] == ["upcoming"]
    assert events[0]["meeting_url"] == "https://meet.google.com/abc-defg-hij"


async def test_a_feed_at_the_cap_is_fetched():
    feed = _feed(4096)
    async with _client(feed) as client:
        text, err = await fetch_ics(URL, client=client, max_bytes=len(feed.encode()))
    assert err is None and text == feed


async def test_a_declared_length_over_the_cap_is_refused_without_reading_the_body():
    data = _feed(2 * MB).encode()
    body = _Body(data)
    async with _client(body, headers={"content-length": str(len(data))}) as client:
        text, err = await fetch_ics(URL, client=client, max_bytes=MB)
    assert text is None and err == "the feed is too large (over 1 MB)"
    assert body.sent <= 64 * 1024


async def test_an_undeclared_body_over_the_cap_is_cut_off_while_streaming():
    data = _feed(3 * MB).encode()
    body = _Body(data)
    async with _client(body) as client:
        text, err = await fetch_ics(URL, client=client, max_bytes=MB)
    assert text is None and err == "the feed is too large (over 1 MB)"
    assert MB < body.sent <= MB + 64 * 1024


async def test_the_cap_counts_decoded_bytes_not_compressed_wire_bytes():
    data = _feed(2 * MB).encode()
    wire = gzip.compress(data)
    assert len(wire) < MB
    headers = {"content-encoding": "gzip", "content-length": str(len(wire))}
    async with _client(wire, headers=headers) as client:
        text, err = await fetch_ics(URL, client=client, max_bytes=MB)
    assert text is None and "too large" in err


@pytest.mark.parametrize("declared", [True, False])
async def test_an_oversized_html_page_gets_the_web_page_message(declared):
    page = ("<!DOCTYPE html><html><head><title>Sign in</title></head><body>"
            + "<div>sign in to continue</div>" * 100_000 + "</body></html>").encode()
    headers = {"content-length": str(len(page))} if declared else {}
    async with _client(_Body(page), headers=headers) as client:
        text, err = await fetch_ics(URL, client=client, max_bytes=MB)
    assert text is None and "web page" in err


async def test_the_operator_cap_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CALENDAR_SYNC_MAX_ICS_BYTES", "2048")
    feed = _feed(4096)
    async with _client(feed) as client:
        text, err = await fetch_ics(URL, client=client)
    assert text is None and err == "the feed is too large (over 2048 bytes)"


def test_the_default_cap_holds_a_multi_year_calendar():
    assert max_ics_bytes("") == DEFAULT_MAX_ICS_BYTES == 32 * MB
    assert max_ics_bytes(" 52428800 ") == 50 * MB


@pytest.mark.parametrize("bad", ["0", "-1", "2MB", "1.5", "abc"])
def test_a_malformed_cap_refuses_the_boot(bad):
    from meeting_api.__main__ import _calendar_max_ics_bytes_at_boot
    from meeting_api.config_preflight import ConfigError

    with pytest.raises(ConfigError, match="CALENDAR_SYNC_MAX_ICS_BYTES"):
        _calendar_max_ics_bytes_at_boot({"CALENDAR_SYNC_MAX_ICS_BYTES": bad})


def test_unset_is_the_default_cap_at_boot():
    from meeting_api.__main__ import _calendar_max_ics_bytes_at_boot

    assert _calendar_max_ics_bytes_at_boot({}) == DEFAULT_MAX_ICS_BYTES
