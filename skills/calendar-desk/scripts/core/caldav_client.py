"""Stdlib WebDAV / CalDAV transport and XML client for skills/calendar-desk.

Implements RFC 4791 CalDAV operations, safe XML parsing (4 MiB cap, entity/doctype blocking),
strict redirect protection, precondition handling (If-None-Match / If-Match),
and automatic injection fencing on returned calendar records.
"""

from __future__ import annotations

import base64
import copy
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from core.ical_codec import expand_record_occurrences, parse_ics_to_records
from core.injection_fence import fence_calendar_record

MAX_XML_BYTES = 4_194_304  # 4 MiB


class CalDAVProtocolError(RuntimeError):
    """Exception raised for CalDAV transport, HTTP, XML, or protocol violations."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int | None = None,
        *,
        secret_to_redact: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self._raw_message = message
        self._secret = secret_to_redact
        super().__init__(self._redact(message))

    def _redact(self, text: str) -> str:
        # Strip Authorization headers or Basic auth tokens
        cleaned = re.sub(r"Basic\s+[A-Za-z0-9+/=]+", "Basic [REDACTED]", text)
        if self._secret:
            cleaned = cleaned.replace(self._secret, "[REDACTED]")
        return cleaned

    def __str__(self) -> str:
        base = self._redact(self._raw_message)
        if self.status_code is not None:
            return f"[{self.code}] HTTP {self.status_code}: {base}"
        return f"[{self.code}] {base}"

    def __repr__(self) -> str:
        return f"CalDAVProtocolError(code={self.code!r}, status_code={self.status_code!r}, message={str(self)!r})"


def parse_dav_xml(raw_xml: bytes | str) -> ET.Element:
    """Safely parse WebDAV / CalDAV XML with entity/doctype scanning and size limits."""
    if isinstance(raw_xml, str):
        raw_bytes = raw_xml.encode("utf-8")
    else:
        raw_bytes = bytes(raw_xml)

    if len(raw_bytes) > MAX_XML_BYTES:
        raise CalDAVProtocolError(
            "CALDAV_OVERSIZED_RESPONSE",
            f"CalDAV XML payload exceeded {MAX_XML_BYTES} bytes ({len(raw_bytes)} bytes)",
        )

    # Security check: disallow entity or doctype declarations (XXE prevention)
    lower = raw_bytes.lower()
    if b"<!doctype" in lower or b"<!entity" in lower:
        raise CalDAVProtocolError(
            "CALDAV_UNSAFE_XML",
            "Unsafe XML payload contains <!DOCTYPE or <!ENTITY declarations",
        )

    try:
        return ET.fromstring(raw_bytes)
    except Exception as exc:
        raise CalDAVProtocolError(
            "CALDAV_XML_PARSE_ERROR",
            f"Failed to parse CalDAV XML: {exc}",
        ) from exc


def _find_elem(parent: ET.Element, *tags: str) -> Optional[ET.Element]:
    for t in tags:
        found = parent.find(t)
        if found is not None:
            return found
    return None


def _find_text(parent: ET.Element, *tags: str) -> Optional[str]:
    elem = _find_elem(parent, *tags)
    if elem is not None and elem.text:
        return elem.text.strip()
    return None


def _make_fenced_record(record: dict[str, Any]) -> dict[str, Any]:
    fenced_res = fence_calendar_record(record)
    fenced_rec = copy.deepcopy(fenced_res["record"])
    fc = fenced_rec.get("fenced_content", {})
    for field in ("summary", "description", "location"):
        if field in fc and isinstance(fc[field], dict) and "fenced" in fc[field]:
            fenced_rec[field] = fc[field]["fenced"]
    return fenced_rec


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom redirect handler preventing cross-origin redirects and HTTPS downgrades."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        orig_parsed = urllib.parse.urlparse(req.get_full_url())
        new_parsed = urllib.parse.urlparse(newurl)

        # Disallow downgrade from https to http
        if orig_parsed.scheme == "https" and new_parsed.scheme == "http":
            raise CalDAVProtocolError(
                "CALDAV_REDIRECT_FORBIDDEN",
                f"Disallowed scheme downgrade on redirect from {req.get_full_url()} to {newurl}",
                status_code=code,
            )

        # Disallow cross-origin redirects
        if orig_parsed.netloc.lower() != new_parsed.netloc.lower():
            raise CalDAVProtocolError(
                "CALDAV_REDIRECT_FORBIDDEN",
                f"Disallowed cross-origin redirect from {orig_parsed.netloc} to {new_parsed.netloc}",
                status_code=code,
            )

        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CalDAVClient:
    """Deterministic, dependency-free CalDAV client implementing RFC 4791."""

    def __init__(
        self,
        principal_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 15.0,
        allow_loopback_http: bool = False,
        default_tz: str = "Europe/Vienna",
    ) -> None:
        self.principal_url = principal_url
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.allow_loopback_http = allow_loopback_http
        self.default_tz = default_tz

        # Validate URL scheme and loopback policy
        parsed = urllib.parse.urlparse(principal_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""

        if scheme == "http":
            is_loopback = host in ("127.0.0.1", "localhost", "::1")
            if not (self.allow_loopback_http and is_loopback):
                raise CalDAVProtocolError(
                    "CALDAV_HTTP_ERROR",
                    f"Insecure HTTP URL '{principal_url}' is not allowed for CalDAVClient",
                )
        elif scheme != "https":
            raise CalDAVProtocolError(
                "CALDAV_HTTP_ERROR",
                f"Unsupported URL scheme '{scheme}' in '{principal_url}'",
            )

        auth_bytes = f"{self.username}:{self.password}".encode("utf-8")
        self._auth_header = f"Basic {base64.b64encode(auth_bytes).decode('ascii')}"

        ssl_ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            SafeRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        )

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        req_headers = {
            "Authorization": self._auth_header,
            "User-Agent": "calendar-desk/1.0",
        }
        if headers:
            req_headers.update(headers)

        full_url = urllib.parse.urljoin(self.principal_url, url)
        req = urllib.request.Request(
            full_url, data=body, headers=req_headers, method=method
        )

        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                resp_bytes = resp.read()
                return status, resp_headers, resp_bytes
        except urllib.error.HTTPError as exc:
            try:
                status = exc.code
                if status == 401:
                    raise CalDAVProtocolError(
                        "CALDAV_AUTH_FAILED",
                        "Authentication failed",
                        status_code=401,
                        secret_to_redact=self.password,
                    ) from exc
                if status == 404:
                    raise CalDAVProtocolError(
                        "CALDAV_NOT_FOUND",
                        f"Resource not found: {full_url}",
                        status_code=404,
                        secret_to_redact=self.password,
                    ) from exc
                if status == 412:
                    raise CalDAVProtocolError(
                        "CALDAV_ETAG_CONFLICT",
                        "Precondition failed (ETag conflict)",
                        status_code=412,
                        secret_to_redact=self.password,
                    ) from exc
                raise CalDAVProtocolError(
                    "CALDAV_HTTP_ERROR",
                    f"HTTP {status} {exc.reason}",
                    status_code=status,
                    secret_to_redact=self.password,
                ) from exc
            finally:
                exc.close()
        except CalDAVProtocolError:
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, CalDAVProtocolError):
                raise exc.reason
            raise CalDAVProtocolError(
                "CALDAV_NETWORK_ERROR",
                f"CalDAV network error connecting to {full_url}: {exc.reason}",
                secret_to_redact=self.password,
            ) from exc

    def discover_calendars(self) -> list[dict[str, Any]]:
        """Discover user principal, calendar home set, and calendar collections."""
        # Step 1: Query principal URL for current-user-principal and calendar-home-set
        status, _, resp_bytes = self._request(
            "PROPFIND",
            self.principal_url,
            body=None,
            headers={
                "Depth": "0",
                "Content-Type": "application/xml; charset=utf-8",
            },
        )
        root = parse_dav_xml(resp_bytes)

        home_set_url: Optional[str] = None
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "calendar-home-set":
                home_set_url = _find_text(elem, ".//{DAV:}href", ".//href")
                if home_set_url:
                    break

        if not home_set_url:
            home_set_url = self.principal_url

        # Step 2: Query calendar home set with Depth: 1 for calendar collections
        _, _, resp_home_bytes = self._request(
            "PROPFIND",
            home_set_url,
            body=None,
            headers={
                "Depth": "1",
                "Content-Type": "application/xml; charset=utf-8",
            },
        )
        root_home = parse_dav_xml(resp_home_bytes)

        calendars: list[dict[str, Any]] = []
        for resp_node in root_home.iter():
            if not resp_node.tag.endswith("response"):
                continue

            cal_href = _find_text(resp_node, ".//{DAV:}href", ".//href")
            if not cal_href:
                continue

            resourcetype_node = _find_elem(
                resp_node, ".//{DAV:}resourcetype", ".//resourcetype"
            )
            if resourcetype_node is None:
                continue

            is_calendar = any(
                child.tag.endswith("calendar")
                for child in resourcetype_node.iter()
            )
            if not is_calendar:
                continue

            display_name = _find_text(
                resp_node, ".//{DAV:}displayname", ".//displayname"
            )
            ctag = _find_text(
                resp_node,
                ".//{http://calendarserver.org/ns/}getctag",
                ".//getctag",
            )
            sync_token = _find_text(
                resp_node, ".//{DAV:}sync-token", ".//sync-token"
            )

            components: list[str] = []
            for comp in resp_node.iter():
                if comp.tag.endswith("comp"):
                    name_attr = comp.get("name")
                    if name_attr:
                        components.append(name_attr.upper())
            if not components:
                components = ["VEVENT"]

            calendars.append(
                {
                    "href": cal_href,
                    "display_name": display_name,
                    "components": components,
                    "ctag": ctag,
                    "sync_token": sync_token,
                }
            )

        return calendars

    def query_events(
        self,
        collection_url: str,
        window_start_utc: str,
        window_end_utc: str,
        *,
        account: str = "default",
        calendar_alias: str = "default",
        expand_recurrences: bool = True,
        max_items: int = 500,
    ) -> dict[str, Any]:
        """Query calendar events via REPORT calendar-query within a time-range."""
        try:
            st_dt = datetime.fromisoformat(window_start_utc.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(window_end_utc.replace("Z", "+00:00"))
        except Exception as exc:
            raise CalDAVProtocolError(
                "CALDAV_INVALID_TIME_RANGE",
                f"Invalid ISO-8601 datetime format for search window: {exc}",
            ) from exc

        if st_dt >= end_dt:
            raise CalDAVProtocolError(
                "CALDAV_INVALID_TIME_RANGE",
                f"Window start ({window_start_utc}) must be strictly before window end ({window_end_utc})",
            )

        if (end_dt - st_dt).total_seconds() > (366 * 86400):
            raise CalDAVProtocolError(
                "CALDAV_INVALID_TIME_RANGE",
                "CalDAV search window cannot exceed 366 days",
            )

        st_utc = st_dt.astimezone(timezone.utc)
        end_utc = end_dt.astimezone(timezone.utc)
        st_compact = st_utc.strftime("%Y%m%dT%H%M%SZ")
        end_compact = end_utc.strftime("%Y%m%dT%H%M%SZ")

        report_body = (
            f'<?xml version="1.0" encoding="utf-8" ?>\n'
            f'<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
            f"  <D:prop>\n"
            f"    <D:getetag/>\n"
            f"    <C:calendar-data/>\n"
            f"  </D:prop>\n"
            f"  <C:filter>\n"
            f'    <C:comp-filter name="VCALENDAR">\n'
            f'      <C:comp-filter name="VEVENT">\n'
            f'        <C:time-range start="{st_compact}" end="{end_compact}"/>\n'
            f"      </C:comp-filter>\n"
            f"    </C:comp-filter>\n"
            f"  </C:filter>\n"
            f"</C:calendar-query>"
        ).encode("utf-8")

        _, _, resp_bytes = self._request(
            "REPORT",
            collection_url,
            body=report_body,
            headers={
                "Depth": "1",
                "Content-Type": "application/xml; charset=utf-8",
            },
        )
        root = parse_dav_xml(resp_bytes)

        all_records: list[dict[str, Any]] = []
        all_fenced: list[dict[str, Any]] = []

        for resp_node in root.iter():
            if not resp_node.tag.endswith("response"):
                continue

            href = _find_text(resp_node, ".//{DAV:}href", ".//href")
            etag = _find_text(resp_node, ".//{DAV:}getetag", ".//getetag")
            data_node = _find_elem(
                resp_node,
                ".//{urn:ietf:params:xml:ns:caldav}calendar-data",
                ".//calendar-data",
            )
            if data_node is None or not data_node.text:
                continue

            raw_ics = data_node.text.strip()
            parsed_records = parse_ics_to_records(
                raw_ics,
                account=account,
                calendar_alias=calendar_alias,
                etag=etag,
                href=href,
                default_tz=self.default_tz,
            )
            if not parsed_records:
                continue

            if expand_recurrences:
                masters = [r for r in parsed_records if not r.get("recurrence_id")]
                overrides = [r for r in parsed_records if r.get("recurrence_id")]

                for master in masters:
                    expanded = expand_record_occurrences(
                        master,
                        window_start_utc,
                        window_end_utc,
                        override_records=overrides,
                        max_instances=max_items,
                    )
                    for rec in expanded:
                        all_records.append(rec)
                        all_fenced.append(_make_fenced_record(rec))
            else:
                for rec in parsed_records:
                    all_records.append(rec)
                    all_fenced.append(_make_fenced_record(rec))

        return {
            "records": all_records[:max_items],
            "fenced_records": all_fenced[:max_items],
            "item_count": len(all_records[:max_items]),
            "window_start_utc": window_start_utc,
            "window_end_utc": window_end_utc,
        }

    def multiget_events(
        self,
        collection_url: str,
        hrefs: Sequence[str],
        *,
        account: str = "default",
        calendar_alias: str = "default",
    ) -> dict[str, Any]:
        """Fetch multiple events by href via REPORT calendar-multiget."""
        href_tags = "".join(f"<D:href>{h}</D:href>" for h in hrefs)
        report_body = (
            f'<?xml version="1.0" encoding="utf-8" ?>\n'
            f'<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
            f"  <D:prop>\n"
            f"    <D:getetag/>\n"
            f"    <C:calendar-data/>\n"
            f"  </D:prop>\n"
            f"  {href_tags}\n"
            f"</C:calendar-multiget>"
        ).encode("utf-8")

        _, _, resp_bytes = self._request(
            "REPORT",
            collection_url,
            body=report_body,
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )
        root = parse_dav_xml(resp_bytes)

        records: list[dict[str, Any]] = []
        fenced_records: list[dict[str, Any]] = []

        for resp_node in root.iter():
            if not resp_node.tag.endswith("response"):
                continue

            href = _find_text(resp_node, ".//{DAV:}href", ".//href")
            etag = _find_text(resp_node, ".//{DAV:}getetag", ".//getetag")
            data_node = _find_elem(
                resp_node,
                ".//{urn:ietf:params:xml:ns:caldav}calendar-data",
                ".//calendar-data",
            )
            if data_node is None or not data_node.text:
                continue

            raw_ics = data_node.text.strip()
            parsed = parse_ics_to_records(
                raw_ics,
                account=account,
                calendar_alias=calendar_alias,
                etag=etag,
                href=href,
                default_tz=self.default_tz,
            )
            for rec in parsed:
                records.append(rec)
                fenced_records.append(_make_fenced_record(rec))

        return {
            "records": records,
            "fenced_records": fenced_records,
            "item_count": len(records),
        }

    def get_event(
        self,
        resource_url: str,
        *,
        account: str = "default",
        calendar_alias: str = "default",
    ) -> dict[str, Any]:
        """Fetch a single .ics event resource via GET."""
        status, headers, body = self._request("GET", resource_url)
        raw_ics = body.decode("utf-8", errors="replace")
        etag = headers.get("ETag") or headers.get("etag")

        parsed = parse_ics_to_records(
            raw_ics,
            account=account,
            calendar_alias=calendar_alias,
            etag=etag,
            href=resource_url,
            default_tz=self.default_tz,
        )
        fenced_list = [_make_fenced_record(r) for r in parsed]

        return {
            "etag": etag,
            "href": resource_url,
            "raw_ics": raw_ics,
            "records": parsed,
            "fenced_records": fenced_list,
        }

    def put_event_ics(
        self,
        resource_url: str,
        raw_ics: str,
        *,
        if_none_match_star: bool = False,
        if_match_etag: str | None = None,
    ) -> dict[str, Any]:
        """Store or update an .ics event resource via PUT with precondition enforcement."""
        if if_none_match_star and if_match_etag is not None:
            raise ValueError(
                "Cannot specify both if_none_match_star and if_match_etag"
            )
        if not if_none_match_star and if_match_etag is None:
            raise ValueError(
                "Must specify either if_none_match_star=True or if_match_etag"
            )

        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if if_none_match_star:
            headers["If-None-Match"] = "*"
        elif if_match_etag is not None:
            etag = if_match_etag.strip()
            if not (etag.startswith('"') and etag.endswith('"')):
                etag = f'"{etag}"'
            headers["If-Match"] = etag

        body = raw_ics.encode("utf-8")
        status, resp_headers, _ = self._request(
            "PUT", resource_url, body=body, headers=headers
        )
        new_etag = resp_headers.get("ETag") or resp_headers.get("etag")

        return {
            "status": "ok",
            "resource_url": resource_url,
            "etag": new_etag,
        }

    def delete_event_resource(
        self,
        resource_url: str,
        *,
        if_match_etag: str,
    ) -> dict[str, Any]:
        """Delete an event resource via DELETE with mandatory If-Match precondition."""
        etag = if_match_etag.strip()
        if not (etag.startswith('"') and etag.endswith('"')):
            etag = f'"{etag}"'

        headers = {"If-Match": etag}
        self._request("DELETE", resource_url, headers=headers)

        return {
            "status": "ok",
            "deleted": True,
            "resource_url": resource_url,
        }
