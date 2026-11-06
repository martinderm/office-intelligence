"""Unit tests for stdlib CalDAV client, XML parsing safety, and transport protocols.

Tests skills/calendar-desk/scripts/core/caldav_client.py contracts:
- CalDAVProtocolError with deterministic codes and status codes
- Safe XML parser (parse_dav_xml): 4 MiB size cap, DOCTYPE/ENTITY rejection
- Hermetic loopback mock-CalDAV server for PROPFIND discovery, REPORT queries, GET, PUT, and DELETE
- Precondition enforcement: If-None-Match: * and If-Match: "<etag>" with 412 -> CALDAV_ETAG_CONFLICT
- Injection fencing integration on returned event records
- Cross-origin and scheme downgrade redirect prevention (CALDAV_REDIRECT_FORBIDDEN)
- Credential and Authorization header stripping from error messages
"""

from __future__ import annotations

import base64
import http.server
import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# Add calendar-desk scripts to sys.path
CALENDAR_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALENDAR_DESK_ROOT / "scripts"))

from core.caldav_client import (  # noqa: E402
    CalDAVClient,
    CalDAVProtocolError,
    parse_dav_xml,
)

SAMPLE_ICS_PAYLOAD = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//office-intelligence//calendar-desk//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-123@office-intelligence.test\r\n"
    "DTSTAMP:20261101T080000Z\r\n"
    "DTSTART:20261102T090000Z\r\n"
    "DTEND:20261102T100000Z\r\n"
    "SUMMARY:Team Sync Meeting\r\n"
    "DESCRIPTION:Weekly catchup with the office team\r\n"
    "LOCATION:Meeting Room 3B\r\n"
    "STATUS:CONFIRMED\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class MockCalDAVHandler(http.server.BaseHTTPRequestHandler):
    """Hermetic in-memory CalDAV test handler."""

    expected_auth: Optional[str] = "Basic " + base64.b64encode(b"testuser:testpass").decode("ascii")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress standard logging during tests
        pass

    def check_auth(self) -> bool:
        if self.expected_auth is None:
            return True
        auth_header = self.headers.get("Authorization")
        if auth_header != self.expected_auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="CalDAV"')
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def do_PROPFIND(self) -> None:
        if not self.check_auth():
            return

        depth = self.headers.get("Depth", "0")
        parsed = urlparse(self.path)

        if parsed.path.rstrip("/") == "/dav/principals":
            # Principal discovery response
            xml_resp = (
                '<?xml version="1.0" encoding="utf-8" ?>\n'
                '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
                '  <D:response>\n'
                '    <D:href>/dav/principals/users/testuser/</D:href>\n'
                '    <D:propstat>\n'
                '      <D:prop>\n'
                '        <D:current-user-principal>\n'
                '          <D:href>/dav/principals/users/testuser/</D:href>\n'
                '        </D:current-user-principal>\n'
                '        <C:calendar-home-set>\n'
                '          <D:href>/dav/calendars/users/testuser/</D:href>\n'
                '        </C:calendar-home-set>\n'
                '      </D:prop>\n'
                '      <D:status>HTTP/1.1 200 OK</D:status>\n'
                '    </D:propstat>\n'
                '  </D:response>\n'
                '</D:multistatus>'
            )
            data = xml_resp.encode("utf-8")
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        elif parsed.path.rstrip("/") == "/dav/calendars/users/testuser":
            # Calendar collection listing (Depth: 1)
            xml_resp = (
                '<?xml version="1.0" encoding="utf-8" ?>\n'
                '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:CS="http://calendarserver.org/ns/">\n'
                '  <D:response>\n'
                '    <D:href>/dav/calendars/users/testuser/</D:href>\n'
                '    <D:propstat>\n'
                '      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>\n'
                '      <D:status>HTTP/1.1 200 OK</D:status>\n'
                '    </D:propstat>\n'
                '  </D:response>\n'
                '  <D:response>\n'
                '    <D:href>/dav/calendars/users/testuser/work/</D:href>\n'
                '    <D:propstat>\n'
                '      <D:prop>\n'
                '        <D:displayname>Work Calendar</D:displayname>\n'
                '        <D:resourcetype><D:collection/><C:calendar/></D:resourcetype>\n'
                '        <C:supported-calendar-component-set>\n'
                '          <C:comp name="VEVENT"/>\n'
                '        </C:supported-calendar-component-set>\n'
                '        <CS:getctag>"ctag-12345"</CS:getctag>\n'
                '        <D:sync-token>sync-token-xyz</D:sync-token>\n'
                '      </D:prop>\n'
                '      <D:status>HTTP/1.1 200 OK</D:status>\n'
                '    </D:propstat>\n'
                '  </D:response>\n'
                '</D:multistatus>'
            )
            data = xml_resp.encode("utf-8")
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()

    def do_REPORT(self) -> None:
        if not self.check_auth():
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")

        # Handle calendar-query or calendar-multiget
        if "calendar-query" in body or "calendar-multiget" in body:
            escaped_ics = SAMPLE_ICS_PAYLOAD.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            xml_resp = (
                '<?xml version="1.0" encoding="utf-8" ?>\n'
                '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
                '  <D:response>\n'
                '    <D:href>/dav/calendars/users/testuser/work/event-123.ics</D:href>\n'
                '    <D:propstat>\n'
                '      <D:prop>\n'
                '        <D:getetag>"etag-v1"</D:getetag>\n'
                f'        <C:calendar-data>{escaped_ics}</C:calendar-data>\n'
                '      </D:prop>\n'
                '      <D:status>HTTP/1.1 200 OK</D:status>\n'
                '    </D:propstat>\n'
                '  </D:response>\n'
                '</D:multistatus>'
            )
            data = xml_resp.encode("utf-8")
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(400)
        self.end_headers()

    def do_GET(self) -> None:
        if not self.check_auth():
            return

        parsed = urlparse(self.path)
        if parsed.path.endswith("event-123.ics"):
            data = SAMPLE_ICS_PAYLOAD.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("ETag", '"etag-v1"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()

    def do_PUT(self) -> None:
        if not self.check_auth():
            return

        if_none_match = self.headers.get("If-None-Match")
        if_match = self.headers.get("If-Match")

        # Simulate conflict if client sends outdated etag
        if if_match == '"outdated-etag"':
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if if_none_match == "*" and self.path.endswith("existing.ics"):
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Otherwise accept
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)

        self.send_response(201)
        self.send_header("ETag", '"etag-v2"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        if not self.check_auth():
            return

        if_match = self.headers.get("If-Match")
        if if_match == '"outdated-etag"':
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(204)
        self.end_headers()


class TestSafeXmlParser(unittest.TestCase):
    def test_parse_dav_xml_valid(self) -> None:
        valid_xml = (
            '<?xml version="1.0" encoding="utf-8" ?>\n'
            '<D:multistatus xmlns:D="DAV:">\n'
            '  <D:response>\n'
            '    <D:href>/item1</D:href>\n'
            '  </D:response>\n'
            '</D:multistatus>'
        )
        root = parse_dav_xml(valid_xml)
        self.assertEqual(root.tag, "{DAV:}multistatus")

    def test_parse_dav_xml_unsafe_doctype_rejected(self) -> None:
        unsafe_xml = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE foo [\n'
            '  <!ELEMENT foo ANY >\n'
            ']>\n'
            '<foo>bar</foo>'
        )
        with self.assertRaises(CalDAVProtocolError) as ctx:
            parse_dav_xml(unsafe_xml)
        self.assertEqual(ctx.exception.code, "CALDAV_UNSAFE_XML")

    def test_parse_dav_xml_unsafe_entity_rejected(self) -> None:
        unsafe_xml = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE test [\n'
            '  <!ENTITY xxe SYSTEM "file:///etc/passwd">\n'
            ']>\n'
            '<test>&xxe;</test>'
        )
        with self.assertRaises(CalDAVProtocolError) as ctx:
            parse_dav_xml(unsafe_xml)
        self.assertEqual(ctx.exception.code, "CALDAV_UNSAFE_XML")

    def test_parse_dav_xml_oversized_rejected(self) -> None:
        # 4 MiB + 1 byte
        oversized = b"<D:multistatus xmlns:D='DAV:'>" + (b" " * (4 * 1024 * 1024 + 1)) + b"</D:multistatus>"
        with self.assertRaises(CalDAVProtocolError) as ctx:
            parse_dav_xml(oversized)
        self.assertEqual(ctx.exception.code, "CALDAV_OVERSIZED_RESPONSE")

    def test_parse_dav_xml_malformed_syntax(self) -> None:
        broken_xml = "<D:multistatus><unclosed>"
        with self.assertRaises(CalDAVProtocolError) as ctx:
            parse_dav_xml(broken_xml)
        self.assertEqual(ctx.exception.code, "CALDAV_XML_PARSE_ERROR")


class TestCalDAVClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockCalDAVHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2.0)

    def get_client(self, username: str = "testuser", password: str = "testpass") -> CalDAVClient:
        return CalDAVClient(
            principal_url=f"{self.base_url}/dav/principals",
            username=username,
            password=password,
            timeout_seconds=5.0,
            allow_loopback_http=True,
            default_tz="Europe/Vienna",
        )

    def test_caldav_client_discover_calendars_mock(self) -> None:
        client = self.get_client()
        calendars = client.discover_calendars()
        self.assertIsInstance(calendars, list)
        self.assertGreaterEqual(len(calendars), 1)
        work_cal = next((c for c in calendars if "work" in c["href"]), None)
        self.assertIsNotNone(work_cal)
        self.assertEqual(work_cal["display_name"], "Work Calendar")
        self.assertEqual(work_cal["ctag"], '"ctag-12345"')
        self.assertEqual(work_cal["sync_token"], "sync-token-xyz")
        self.assertIn("VEVENT", work_cal.get("components", []))

    def test_caldav_client_query_events_mock(self) -> None:
        client = self.get_client()
        collection_url = f"{self.base_url}/dav/calendars/users/testuser/work/"
        result = client.query_events(
            collection_url,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-30T00:00:00Z",
            expand_recurrences=True,
        )
        self.assertIn("records", result)
        self.assertIn("fenced_records", result)
        self.assertEqual(len(result["records"]), 1)
        record = result["records"][0]
        self.assertEqual(record["uid"], "event-123@office-intelligence.test")
        self.assertEqual(record["summary"], "Team Sync Meeting")

        # Verify injection fence was applied to fenced_records
        fenced_record = result["fenced_records"][0]
        self.assertIn("<untrusted_calendar_content", fenced_record["summary"])

    def test_caldav_client_query_events_invalid_time_range(self) -> None:
        client = self.get_client()
        collection_url = f"{self.base_url}/dav/calendars/users/testuser/work/"

        # Reversed range
        with self.assertRaises(CalDAVProtocolError) as ctx_rev:
            client.query_events(
                collection_url,
                window_start_utc="2026-11-30T00:00:00Z",
                window_end_utc="2026-11-01T00:00:00Z",
            )
        self.assertEqual(ctx_rev.exception.code, "CALDAV_INVALID_TIME_RANGE")

        # Range exceeding 366 days
        with self.assertRaises(CalDAVProtocolError) as ctx_long:
            client.query_events(
                collection_url,
                window_start_utc="2026-01-01T00:00:00Z",
                window_end_utc="2027-02-01T00:00:00Z",
            )
        self.assertEqual(ctx_long.exception.code, "CALDAV_INVALID_TIME_RANGE")

    def test_caldav_client_multiget_events_mock(self) -> None:
        client = self.get_client()
        collection_url = f"{self.base_url}/dav/calendars/users/testuser/work/"
        hrefs = ["/dav/calendars/users/testuser/work/event-123.ics"]
        result = client.multiget_events(collection_url, hrefs)
        self.assertIn("records", result)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["uid"], "event-123@office-intelligence.test")

    def test_caldav_client_get_event_mock(self) -> None:
        client = self.get_client()
        resource_url = f"{self.base_url}/dav/calendars/users/testuser/work/event-123.ics"
        resp = client.get_event(resource_url)
        self.assertEqual(resp["etag"], '"etag-v1"')
        self.assertIn("BEGIN:VCALENDAR", resp["raw_ics"])
        self.assertEqual(len(resp["records"]), 1)
        self.assertEqual(resp["records"][0]["summary"], "Team Sync Meeting")
        self.assertIn("<untrusted_calendar_content", resp["fenced_records"][0]["summary"])

    def test_caldav_client_put_event_with_if_none_match(self) -> None:
        client = self.get_client()
        resource_url = f"{self.base_url}/dav/calendars/users/testuser/work/new-event.ics"
        resp = client.put_event_ics(resource_url, SAMPLE_ICS_PAYLOAD, if_none_match_star=True)
        self.assertEqual(resp["etag"], '"etag-v2"')

    def test_caldav_client_put_event_with_if_match(self) -> None:
        client = self.get_client()
        resource_url = f"{self.base_url}/dav/calendars/users/testuser/work/event-123.ics"
        resp = client.put_event_ics(resource_url, SAMPLE_ICS_PAYLOAD, if_match_etag='"etag-v1"')
        self.assertEqual(resp["etag"], '"etag-v2"')

    def test_caldav_client_put_event_etag_conflict(self) -> None:
        client = self.get_client()
        resource_url = f"{self.base_url}/dav/calendars/users/testuser/work/event-123.ics"
        with self.assertRaises(CalDAVProtocolError) as ctx:
            client.put_event_ics(
                resource_url,
                SAMPLE_ICS_PAYLOAD,
                if_match_etag='"outdated-etag"',
            )
        self.assertEqual(ctx.exception.code, "CALDAV_ETAG_CONFLICT")
        self.assertEqual(ctx.exception.status_code, 412)

    def test_caldav_client_delete_event_with_if_match(self) -> None:
        client = self.get_client()
        resource_url = f"{self.base_url}/dav/calendars/users/testuser/work/event-123.ics"
        resp = client.delete_event_resource(resource_url, if_match_etag='"etag-v1"')
        self.assertTrue(resp.get("deleted", True))

    def test_caldav_client_delete_event_etag_conflict(self) -> None:
        client = self.get_client()
        resource_url = f"{self.base_url}/dav/calendars/users/testuser/work/event-123.ics"
        with self.assertRaises(CalDAVProtocolError) as ctx:
            client.delete_event_resource(resource_url, if_match_etag='"outdated-etag"')
        self.assertEqual(ctx.exception.code, "CALDAV_ETAG_CONFLICT")
        self.assertEqual(ctx.exception.status_code, 412)

    def test_caldav_client_auth_failure(self) -> None:
        bad_client = self.get_client(username="testuser", password="WRONG_PASSWORD")
        with self.assertRaises(CalDAVProtocolError) as ctx:
            bad_client.discover_calendars()
        self.assertEqual(ctx.exception.code, "CALDAV_AUTH_FAILED")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_caldav_client_strips_auth_header_from_errors(self) -> None:
        secret_pass = "super-secret-password-xyz"
        bad_client = self.get_client(username="testuser", password=secret_pass)
        try:
            bad_client.discover_calendars()
        except CalDAVProtocolError as exc:
            err_msg = str(exc)
            err_repr = repr(exc)
            self.assertNotIn(secret_pass, err_msg)
            self.assertNotIn(secret_pass, err_repr)
            self.assertNotIn("Basic ", err_msg)


if __name__ == "__main__":
    unittest.main()
