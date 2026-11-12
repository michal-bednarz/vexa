"""Production I/O for calendar sync — the ICS fetch (SSRF-pinned) + the config discovery hop.

``fetch_ics`` dereferences a USER-SUPPLIED URL server-side, so it MUST ride the same pinned
transport the webhook sender uses (``webhooks/ssrf.build_pinned_transport``): the host is
resolved + validated at connect time and the socket dials the validated IP — a DNS-rebinding
flip can never turn the poller into an internal-network probe. Size-capped: the body is streamed
and the fetch stops at ``CALENDAR_SYNC_MAX_ICS_BYTES`` decoded bytes (default
``DEFAULT_MAX_ICS_BYTES``), and a declared ``Content-Length`` over the cap is refused before the
body is read — an oversize feed is refused, never held in memory whole, never parsed.

``fetch_configs`` asks admin-api's internal edge (X-Internal-Secret) which users have a feed
connected — the secret URL crosses only this internal hop.
"""
from __future__ import annotations

import os
from typing import Optional

#: A secret iCal address (Google, Outlook) exports the WHOLE calendar — every past event, with no
#: time-range parameter — so a work calendar with years of meetings runs to several megabytes even
#: though ``parse_ics`` keeps a two-week window. The cap bounds one fetch's memory; it is not an
#: estimate of a plausible calendar. Operators may raise or lower it.
MAX_ICS_BYTES_ENV = "CALENDAR_SYNC_MAX_ICS_BYTES"
DEFAULT_MAX_ICS_BYTES = 32 * 1024 * 1024

_WEB_PAGE = ("the URL returns a web page, not a calendar feed — in Google Calendar use "
             "Settings → Integrate calendar → 'Secret address in iCal format' (ends in .ics)")
_HEAD_BYTES = 4096

#: The operator's list of internal hosts and networks a calendar feed may be served from (a feed
#: inside a corporate network). Deployment configuration only — no user can set or widen it, and it
#: applies to calendar feeds and nothing else. admin-api reads the same key when a feed is saved.
FEED_ALLOW_ENV = "VEXA_CALENDAR_FEED_ALLOW"


def feed_allowance(raw: Optional[str] = None):
    """The ``OperatorAllowance`` for calendar feeds, parsed from ``VEXA_CALENDAR_FEED_ALLOW``
    (``raw`` is the test seam). Raises ``ValueError`` naming a bad entry; the boot calls this once
    so a typo refuses the boot instead of silently refusing every internal feed."""
    from ..webhooks.ssrf import parse_allowance

    value = os.getenv(FEED_ALLOW_ENV, "") if raw is None else raw
    return parse_allowance(value, what=FEED_ALLOW_ENV)


def max_ics_bytes(raw: Optional[str] = None) -> int:
    """The decoded-size cap on one feed, from ``CALENDAR_SYNC_MAX_ICS_BYTES`` (``raw`` is the test
    seam); unset or empty is ``DEFAULT_MAX_ICS_BYTES``. Raises ``ValueError`` naming the key for
    anything but a positive whole number of bytes — the boot calls this once."""
    value = (os.getenv(MAX_ICS_BYTES_ENV, "") if raw is None else raw).strip()
    if not value:
        return DEFAULT_MAX_ICS_BYTES
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{MAX_ICS_BYTES_ENV} must be a positive whole number of bytes, got {value!r}")
    return int(value)


def _size_label(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.1f}".rstrip("0").rstrip(".") + " MB" if mb >= 0.1 else f"{n} bytes"


def _looks_like_html(head: str) -> bool:
    head = head.lstrip()[:200].lower()
    return head.startswith("<") or "<html" in head


def build_ics_client(*, timeout_s: float = 15.0, allow=None):
    """A pinned httpx client for ICS fetches — the ONE place the transport is configured.

    A sweep that reuses a single client across a tick's feeds keeps connection pooling and TLS
    handshakes amortized; ``fetch_ics`` builds a per-call one when no client is passed. ``allow``
    defaults to the operator's calendar-feed allowance; the transport still pins every request
    and re-checks at connect, so an allowed host that re-resolves elsewhere is refused.
    """
    import httpx

    from ..webhooks.ssrf import build_pinned_transport

    return httpx.AsyncClient(
        timeout=timeout_s,
        transport=build_pinned_transport(allow=feed_allowance() if allow is None else allow),
        follow_redirects=False,
    )


async def fetch_ics(url: str, *, timeout_s: float = 15.0, client=None,
                    max_bytes: Optional[int] = None) -> tuple[Optional[str], Optional[str]]:
    """GET the ICS feed over the SSRF-pinned transport → ``(feed_text, None)`` on success or
    ``(None, human_reason)`` on any failure. The reason is USER-FACING (it becomes the feed's
    ``last_error`` and is shown in the terminal's calendar panel), so it names the actual
    problem — an HTML page instead of a feed, a bad status, oversize — never a stack trace.

    ``client`` (optional) is a caller-owned pinned client — pass one to share a connection pool
    across a sweep; the caller owns its lifetime. ``max_bytes`` defaults to ``max_ics_bytes()``."""
    limit = max_ics_bytes() if max_bytes is None else max_bytes
    try:
        if client is not None:
            return await _read_feed(client, url, limit)
        async with build_ics_client(timeout_s=timeout_s) as owned:
            return await _read_feed(owned, url, limit)
    except Exception:
        return None, "couldn't reach the URL (unreachable, timed out, or a blocked/internal address)"


async def _read_feed(client, url: str, limit: int) -> tuple[Optional[str], Optional[str]]:
    async with client.stream("GET", url) as resp:
        if resp.status_code in (301, 302, 303, 307, 308):
            return None, "the URL redirects — paste the final feed URL (Google: the 'Secret address in iCal format')"
        if resp.status_code != 200:
            return None, f"the URL answered HTTP {resp.status_code}"
        encoding = resp.encoding or "utf-8"
        declared = resp.headers.get("content-length", "")
        # Content-Length counts wire bytes, which a compressed feed never exceeds once decoded, so
        # a declared size over the cap is refused on the head alone — read so that an HTML page
        # still gets the message that tells the user which URL to paste instead.
        declared_over = declared.isdigit() and int(declared) > limit
        stop_after = _HEAD_BYTES if declared_over else limit
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body.extend(chunk)
            if len(body) > stop_after:
                break
        if _looks_like_html(bytes(body[:_HEAD_BYTES]).decode(encoding, errors="replace")):
            return None, _WEB_PAGE
        if declared_over or len(body) > limit:
            return None, f"the feed is too large (over {_size_label(limit)})"
        text = body.decode(encoding, errors="replace")
        if "begin:vcalendar" not in text.lstrip()[:200].lower():
            return None, "the URL doesn't return an ICS calendar (no BEGIN:VCALENDAR)"
        return text, None


async def fetch_configs(admin_api_url: str, internal_secret: str,
                        *, timeout_s: float = 10.0) -> Optional[list[dict]]:
    """``[{user_id, ics_url, auto_join}]`` from admin-api's internal calendar-configs edge, or
    ``None`` when identity is unreachable (the sweep skips the tick — fail-closed, not fail-silent)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(
                f"{admin_api_url.rstrip('/')}/internal/calendar-configs",
                headers={"X-Internal-Secret": internal_secret},
            )
        if resp.status_code != 200:
            return None
        body = resp.json()
        configs = body.get("configs") if isinstance(body, dict) else None
        return configs if isinstance(configs, list) else None
    except Exception:
        return None
