"""Backend configuration and credential resolution for skills/calendar-desk.

Handles validation of .agents/calendar-desk-backend.json (schema "calendar-desk-backend/1"),
secure credential resolution from .agents/secrets.json or environment variables,
userinfo URL rejection, loopback-only HTTP policy, and secret redaction.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


class CalDAVConfigError(ValueError):
    """Exception raised for configuration or credential resolution errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AccountCredentials:
    """Resolved credentials for a CalDAV account.

    Guarantees raw passwords are redacted in repr(), str(), and to_public_dict().
    """

    account_id: str
    principal_url: str
    username: str
    password: str
    allow_loopback_http: bool = False
    timeout_seconds: float = 15.0

    def __repr__(self) -> str:
        return (
            f"AccountCredentials(account_id={self.account_id!r}, "
            f"principal_url={self.principal_url!r}, "
            f"username={self.username!r}, "
            f"password='***', "
            f"allow_loopback_http={self.allow_loopback_http!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def to_public_dict(self) -> dict[str, Any]:
        """Return non-sensitive public metadata without secrets."""
        return {
            "account_id": self.account_id,
            "principal_url": self.principal_url,
            "username": self.username,
            "allow_loopback_http": self.allow_loopback_http,
            "timeout_seconds": self.timeout_seconds,
        }


def load_backend_config(path: Path | str) -> dict[str, Any]:
    """Load and validate .agents/calendar-desk-backend.json."""
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise CalDAVConfigError(
            "CALDAV_CONFIG_MISSING",
            f"Calendar desk backend config file not found: {cfg_path.as_posix()}",
        )

    try:
        raw_text = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as exc:
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            f"Calendar desk backend config is not valid JSON: {exc}",
        ) from exc

    if not isinstance(data, dict):
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            "Calendar desk backend config root must be a JSON object",
        )

    if data.get("schema") != "calendar-desk-backend/1":
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            f"Unsupported schema version in backend config: {data.get('schema')!r}; expected 'calendar-desk-backend/1'",
        )

    default_account = data.get("default_account")
    if not default_account or not isinstance(default_account, str):
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            "Backend config must declare a non-empty string 'default_account'",
        )

    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            "Backend config must contain an 'accounts' mapping",
        )

    if default_account not in accounts:
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            f"Default account '{default_account}' is not defined in 'accounts'",
        )

    # Defaults
    data.setdefault("default_timezone", "Europe/Vienna")
    return data


def resolve_account_credentials(
    account_id: str,
    backend_cfg: Mapping[str, Any],
    *,
    secrets_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> AccountCredentials:
    """Resolve credentials for account_id from secrets.json or environment variables."""
    accounts = backend_cfg.get("accounts", {})
    if account_id not in accounts:
        raise CalDAVConfigError(
            "CALDAV_ACCOUNT_NOT_FOUND",
            f"CalDAV account '{account_id}' is not defined in backend config",
        )

    acct_meta = accounts[account_id] or {}
    allow_loopback_http = bool(acct_meta.get("allow_loopback_http", False))
    timeout_seconds = float(acct_meta.get("timeout_seconds", 15.0))
    if timeout_seconds < 1.0 or timeout_seconds > 60.0:
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            f"timeout_seconds for account '{account_id}' must be between 1.0 and 60.0; got {timeout_seconds}",
        )

    principal_url = acct_meta.get("principal_url")
    username: Optional[str] = None
    password: Optional[str] = None

    # 1. Try reading secrets file
    s_path = Path(secrets_path) if secrets_path else Path(".agents/secrets.json")
    if s_path.is_file():
        try:
            secrets_raw = json.loads(s_path.read_text(encoding="utf-8"))
            caldav_secrets = (
                secrets_raw.get("integrations", {})
                .get("caldav", {})
                .get("accounts", {})
                .get(account_id, {})
            )
            if isinstance(caldav_secrets, dict):
                if not principal_url and caldav_secrets.get("principal_url"):
                    principal_url = caldav_secrets["principal_url"]
                if caldav_secrets.get("username"):
                    username = caldav_secrets["username"]
                if caldav_secrets.get("password"):
                    password = caldav_secrets["password"]
                elif caldav_secrets.get("app_password"):
                    password = caldav_secrets["app_password"]
        except Exception:
            pass

    # 2. Try environment fallback
    environment = env if env is not None else os.environ
    upper_key = account_id.upper().replace("-", "_")

    if not principal_url:
        principal_url = environment.get(f"CALDAV_PRINCIPAL_URL_{upper_key}")
    if not username:
        username = environment.get(f"CALDAV_USERNAME_{upper_key}")
    if not password:
        password = environment.get(f"CALDAV_PASSWORD_{upper_key}")

    if not username or not password:
        raise CalDAVConfigError(
            "CALDAV_SECRET_MISSING",
            f"Missing username or password for CalDAV account '{account_id}' in secrets.json and environment",
        )

    if not principal_url:
        raise CalDAVConfigError(
            "CALDAV_CONFIG_INVALID",
            f"Missing principal_url for CalDAV account '{account_id}'",
        )

    # 3. Security checks on principal_url
    parsed = urllib.parse.urlparse(principal_url)

    # Reject embedded userinfo: user:pass@host
    if parsed.username or parsed.password or ("@" in parsed.netloc):
        raise CalDAVConfigError(
            "CALDAV_USERINFO_FORBIDDEN",
            f"Principal URL for account '{account_id}' must not contain embedded user:pass credentials",
        )

    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""

    if scheme == "http":
        is_loopback = host in ("127.0.0.1", "localhost", "::1")
        if not (allow_loopback_http and is_loopback):
            raise CalDAVConfigError(
                "CALDAV_INSECURE_URL",
                f"Insecure HTTP URL '{principal_url}' is forbidden. CalDAV requires HTTPS (loopback HTTP allowed only when allow_loopback_http=True on 127.0.0.1/::1/localhost)",
            )
    elif scheme != "https":
        raise CalDAVConfigError(
            "CALDAV_INSECURE_URL",
            f"Unsupported URL scheme '{scheme}' in '{principal_url}'; must be https://",
        )

    return AccountCredentials(
        account_id=account_id,
        principal_url=principal_url,
        username=username,
        password=password,
        allow_loopback_http=allow_loopback_http,
        timeout_seconds=timeout_seconds,
    )


def resolve_calendar_target(
    backend_cfg: Mapping[str, Any],
    *,
    account_id: str | None = None,
    calendar_alias: str | None = None,
    secrets_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve full calendar target details, combining collection URL and credentials."""
    target_account = account_id or backend_cfg["default_account"]
    credentials = resolve_account_credentials(
        target_account, backend_cfg, secrets_path=secrets_path, env=env
    )

    accounts = backend_cfg.get("accounts", {})
    acct_meta = accounts[target_account] or {}
    calendars = acct_meta.get("calendars", {})

    target_alias = calendar_alias or "primary"
    if target_alias not in calendars:
        # Fall back to single calendar if only one is registered and alias was not explicitly passed
        if calendar_alias is None and len(calendars) == 1:
            target_alias = next(iter(calendars))
        else:
            raise CalDAVConfigError(
                "CALDAV_CALENDAR_NOT_FOUND",
                f"Calendar alias '{target_alias}' not found in account '{target_account}'",
            )

    cal_meta = calendars[target_alias] or {}

    # Compute absolute collection URL
    if cal_meta.get("collection_url"):
        collection_url = urllib.parse.urljoin(
            credentials.principal_url, cal_meta["collection_url"]
        )
    else:
        path = cal_meta.get("collection_path", "")
        collection_url = urllib.parse.urljoin(credentials.principal_url, path)

    # Invariants on collection_url
    parsed_coll = urllib.parse.urlparse(collection_url)
    if parsed_coll.username or parsed_coll.password or ("@" in parsed_coll.netloc):
        raise CalDAVConfigError(
            "CALDAV_USERINFO_FORBIDDEN",
            f"Calendar collection URL '{collection_url}' contains forbidden embedded credentials",
        )

    coll_scheme = parsed_coll.scheme.lower()
    coll_host = (parsed_coll.hostname or "").lower()
    coll_port = parsed_coll.port or (80 if coll_scheme == "http" else 443)

    if coll_scheme == "http":
        is_loopback = coll_host in ("127.0.0.1", "localhost", "::1")
        if not (credentials.allow_loopback_http and is_loopback):
            raise CalDAVConfigError(
                "CALDAV_INSECURE_URL",
                f"Insecure HTTP URL '{collection_url}' is forbidden. CalDAV requires HTTPS (loopback HTTP allowed only when allow_loopback_http=True on 127.0.0.1/::1/localhost)",
            )
    elif coll_scheme != "https":
        raise CalDAVConfigError(
            "CALDAV_INSECURE_URL",
            f"Unsupported URL scheme '{coll_scheme}' in '{collection_url}'; must be https://",
        )

    parsed_princ = urllib.parse.urlparse(credentials.principal_url)
    princ_scheme = parsed_princ.scheme.lower()
    princ_host = (parsed_princ.hostname or "").lower()
    princ_port = parsed_princ.port or (80 if princ_scheme == "http" else 443)

    if (coll_scheme, coll_host, coll_port) != (princ_scheme, princ_host, princ_port):
        raise CalDAVConfigError(
            "CALDAV_CROSS_ORIGIN_URL",
            f"Calendar collection URL origin '{(coll_scheme, coll_host, coll_port)}' does not match principal URL origin '{(princ_scheme, princ_host, princ_port)}'",
        )

    role = cal_meta.get("role", "primary")
    write_policy = cal_meta.get("write_policy", "read_only")
    default_tz = (
        cal_meta.get("default_timezone")
        or acct_meta.get("default_timezone")
        or backend_cfg.get("default_timezone", "Europe/Vienna")
    )

    return {
        "account_id": target_account,
        "calendar_alias": target_alias,
        "collection_url": collection_url,
        "display_name": cal_meta.get("display_name"),
        "role": role,
        "write_policy": write_policy,
        "default_timezone": default_tz,
        "credentials": credentials,
    }
