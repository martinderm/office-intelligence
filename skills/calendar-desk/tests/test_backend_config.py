"""Unit tests for CalDAV backend configuration and credential resolution.

Tests skills/calendar-desk/scripts/core/backend_config.py contracts:
- CalDAVConfigError with machine-readable error codes
- load_backend_config schema validation (.agents/calendar-desk-backend.json)
- resolve_account_credentials resolution from secrets.json and env vars
- Security invariants: URL userinfo rejection, loopback-only HTTP policy, credential redaction
- resolve_calendar_target target resolution with collection URLs
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

# Add calendar-desk scripts to sys.path
CALENDAR_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALENDAR_DESK_ROOT / "scripts"))

from core.backend_config import (  # noqa: E402
    AccountCredentials,
    CalDAVConfigError,
    load_backend_config,
    resolve_account_credentials,
    resolve_calendar_target,
)


class TestBackendConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_backend_config_valid(self) -> None:
        cfg_data = {
            "schema": "calendar-desk-backend/1",
            "default_account": "infomaniak-work",
            "default_timezone": "Europe/Vienna",
            "accounts": {
                "infomaniak-work": {
                    "principal_url": "https://cal.infomaniak.com/caldav/v2/",
                    "allow_loopback_http": False,
                    "timeout_seconds": 20.0,
                    "calendars": {
                        "primary": {
                            "collection_path": "/caldav/v2/user@example.com/calendar/",
                            "display_name": "Personal Calendar",
                            "role": "primary",
                            "write_policy": "dry_run_and_approval",
                            "default_timezone": "Europe/Vienna",
                        }
                    },
                }
            },
        }
        cfg_file = self.dir_path / "backend.json"
        cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

        loaded = load_backend_config(cfg_file)
        self.assertEqual(loaded["schema"], "calendar-desk-backend/1")
        self.assertEqual(loaded["default_account"], "infomaniak-work")
        self.assertIn("infomaniak-work", loaded["accounts"])
        self.assertEqual(
            loaded["accounts"]["infomaniak-work"]["calendars"]["primary"]["role"],
            "primary",
        )

    def test_load_backend_config_missing_file(self) -> None:
        missing_file = self.dir_path / "non_existent.json"
        with self.assertRaises(CalDAVConfigError) as ctx:
            load_backend_config(missing_file)
        self.assertEqual(ctx.exception.code, "CALDAV_CONFIG_MISSING")

    def test_load_backend_config_invalid_json(self) -> None:
        bad_json_file = self.dir_path / "corrupt.json"
        bad_json_file.write_text("{this is not json}", encoding="utf-8")
        with self.assertRaises(CalDAVConfigError) as ctx:
            load_backend_config(bad_json_file)
        self.assertEqual(ctx.exception.code, "CALDAV_CONFIG_INVALID")

    def test_load_backend_config_invalid_schema(self) -> None:
        bad_schema = {
            "schema": "unknown-schema/99",
            "default_account": "work",
            "accounts": {"work": {}},
        }
        cfg_file = self.dir_path / "bad_schema.json"
        cfg_file.write_text(json.dumps(bad_schema), encoding="utf-8")
        with self.assertRaises(CalDAVConfigError) as ctx:
            load_backend_config(cfg_file)
        self.assertEqual(ctx.exception.code, "CALDAV_CONFIG_INVALID")

    def test_load_backend_config_missing_default_account(self) -> None:
        bad_cfg = {
            "schema": "calendar-desk-backend/1",
            "accounts": {"work": {}},
        }
        cfg_file = self.dir_path / "missing_default.json"
        cfg_file.write_text(json.dumps(bad_cfg), encoding="utf-8")
        with self.assertRaises(CalDAVConfigError) as ctx:
            load_backend_config(cfg_file)
        self.assertEqual(ctx.exception.code, "CALDAV_CONFIG_INVALID")

    def test_load_backend_config_default_account_not_in_accounts(self) -> None:
        bad_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {"other": {}},
        }
        cfg_file = self.dir_path / "mismatched_default.json"
        cfg_file.write_text(json.dumps(bad_cfg), encoding="utf-8")
        with self.assertRaises(CalDAVConfigError) as ctx:
            load_backend_config(cfg_file)
        self.assertEqual(ctx.exception.code, "CALDAV_CONFIG_INVALID")

    def test_resolve_account_credentials_from_secrets_file(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {
                "work": {
                    "principal_url": "https://cal.example.com/dav/",
                    "timeout_seconds": 15.0,
                }
            },
        }
        secrets_data = {
            "integrations": {
                "caldav": {
                    "accounts": {
                        "work": {
                            "principal_url": "https://cal.example.com/dav/",
                            "username": "martin@example.com",
                            "password": "super-secret-password-123",
                        }
                    }
                }
            }
        }
        secrets_file = self.dir_path / "secrets.json"
        secrets_file.write_text(json.dumps(secrets_data), encoding="utf-8")

        creds = resolve_account_credentials(
            "work", backend_cfg, secrets_path=secrets_file
        )
        self.assertIsInstance(creds, AccountCredentials)
        self.assertEqual(creds.account_id, "work")
        self.assertEqual(creds.username, "martin@example.com")
        self.assertEqual(creds.password, "super-secret-password-123")
        self.assertEqual(creds.principal_url, "https://cal.example.com/dav/")
        self.assertFalse(creds.allow_loopback_http)

    def test_resolve_account_credentials_with_app_password(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {"work": {"principal_url": "https://cal.example.com/dav/"}},
        }
        secrets_data = {
            "integrations": {
                "caldav": {
                    "accounts": {
                        "work": {
                            "username": "appuser@example.com",
                            "app_password": "app-token-xyz-456",
                        }
                    }
                }
            }
        }
        secrets_file = self.dir_path / "secrets.json"
        secrets_file.write_text(json.dumps(secrets_data), encoding="utf-8")

        creds = resolve_account_credentials(
            "work", backend_cfg, secrets_path=secrets_file
        )
        self.assertEqual(creds.username, "appuser@example.com")
        self.assertEqual(creds.password, "app-token-xyz-456")

    def test_resolve_account_credentials_from_env_fallback(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "fastmail",
            "accounts": {"fastmail": {}},
        }
        mock_env = {
            "CALDAV_USERNAME_FASTMAIL": "fastuser@fastmail.com",
            "CALDAV_PASSWORD_FASTMAIL": "fastmail-app-pass",
            "CALDAV_PRINCIPAL_URL_FASTMAIL": "https://caldav.fastmail.com/dav/",
        }
        creds = resolve_account_credentials("fastmail", backend_cfg, env=mock_env)
        self.assertEqual(creds.account_id, "fastmail")
        self.assertEqual(creds.username, "fastuser@fastmail.com")
        self.assertEqual(creds.password, "fastmail-app-pass")
        self.assertEqual(creds.principal_url, "https://caldav.fastmail.com/dav/")

    def test_resolve_account_credentials_missing_secret(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {"work": {"principal_url": "https://cal.example.com/dav/"}},
        }
        secrets_data = {
            "integrations": {
                "caldav": {
                    "accounts": {
                        "work": {
                            "username": "martin@example.com"
                            # password missing
                        }
                    }
                }
            }
        }
        secrets_file = self.dir_path / "secrets.json"
        secrets_file.write_text(json.dumps(secrets_data), encoding="utf-8")

        with self.assertRaises(CalDAVConfigError) as ctx:
            resolve_account_credentials(
                "work", backend_cfg, secrets_path=secrets_file, env={}
            )
        self.assertEqual(ctx.exception.code, "CALDAV_SECRET_MISSING")

    def test_resolve_account_credentials_account_not_found(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {"work": {}},
        }
        with self.assertRaises(CalDAVConfigError) as ctx:
            resolve_account_credentials("unknown", backend_cfg)
        self.assertEqual(ctx.exception.code, "CALDAV_ACCOUNT_NOT_FOUND")

    def test_resolve_account_credentials_userinfo_forbidden(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {
                "work": {
                    "principal_url": "https://user:secret@cal.example.com/dav/"
                }
            },
        }
        mock_env = {
            "CALDAV_USERNAME_WORK": "user",
            "CALDAV_PASSWORD_WORK": "secret",
        }
        with self.assertRaises(CalDAVConfigError) as ctx:
            resolve_account_credentials("work", backend_cfg, env=mock_env)
        self.assertEqual(ctx.exception.code, "CALDAV_USERINFO_FORBIDDEN")

    def test_resolve_account_credentials_insecure_http_rejected(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {
                "work": {
                    "principal_url": "http://cal.example.com/dav/",
                    "allow_loopback_http": False,
                }
            },
        }
        mock_env = {
            "CALDAV_USERNAME_WORK": "user",
            "CALDAV_PASSWORD_WORK": "secret",
        }
        with self.assertRaises(CalDAVConfigError) as ctx:
            resolve_account_credentials("work", backend_cfg, env=mock_env)
        self.assertEqual(ctx.exception.code, "CALDAV_INSECURE_URL")

    def test_resolve_account_credentials_loopback_http_allowed(self) -> None:
        for loopback_host in ["127.0.0.1:8080", "localhost:8080", "[::1]:8080"]:
            backend_cfg = {
                "schema": "calendar-desk-backend/1",
                "default_account": "local",
                "accounts": {
                    "local": {
                        "principal_url": f"http://{loopback_host}/dav/",
                        "allow_loopback_http": True,
                    }
                },
            }
            mock_env = {
                "CALDAV_USERNAME_LOCAL": "user",
                "CALDAV_PASSWORD_LOCAL": "secret",
            }
            creds = resolve_account_credentials("local", backend_cfg, env=mock_env)
            self.assertTrue(creds.allow_loopback_http)
            self.assertEqual(creds.principal_url, f"http://{loopback_host}/dav/")

    def test_resolve_account_credentials_loopback_http_rejected_when_flag_false(
        self,
    ) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "local",
            "accounts": {
                "local": {
                    "principal_url": "http://127.0.0.1:8080/dav/",
                    "allow_loopback_http": False,
                }
            },
        }
        mock_env = {
            "CALDAV_USERNAME_LOCAL": "user",
            "CALDAV_PASSWORD_LOCAL": "secret",
        }
        with self.assertRaises(CalDAVConfigError) as ctx:
            resolve_account_credentials("local", backend_cfg, env=mock_env)
        self.assertEqual(ctx.exception.code, "CALDAV_INSECURE_URL")

    def test_account_credentials_redaction(self) -> None:
        creds = AccountCredentials(
            account_id="work",
            principal_url="https://cal.example.com/dav/",
            username="admin@example.com",
            password="my-super-confidential-password",
            allow_loopback_http=False,
            timeout_seconds=15.0,
        )
        raw_repr = repr(creds)
        raw_str = str(creds)
        public_dict = creds.to_public_dict()

        self.assertNotIn("my-super-confidential-password", raw_repr)
        self.assertNotIn("my-super-confidential-password", raw_str)
        self.assertNotIn(
            "my-super-confidential-password", json.dumps(public_dict)
        )
        self.assertEqual(public_dict.get("username"), "admin@example.com")
        self.assertEqual(public_dict.get("account_id"), "work")

    def test_resolve_account_credentials_timeout_out_of_bounds(self) -> None:
        mock_env = {
            "CALDAV_USERNAME_WORK": "user",
            "CALDAV_PASSWORD_WORK": "secret",
        }
        for bad_timeout in [0.5, 0.0, -5.0, 65.0, 100.0]:
            backend_cfg = {
                "schema": "calendar-desk-backend/1",
                "default_account": "work",
                "accounts": {
                    "work": {
                        "principal_url": "https://cal.example.com/dav/",
                        "timeout_seconds": bad_timeout,
                    }
                },
            }
            with self.assertRaises(CalDAVConfigError) as ctx:
                resolve_account_credentials("work", backend_cfg, env=mock_env)
            self.assertEqual(ctx.exception.code, "CALDAV_CONFIG_INVALID")

    def test_resolve_calendar_target_default_and_explicit(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "default_timezone": "Europe/Vienna",
            "accounts": {
                "work": {
                    "principal_url": "https://cal.example.com/dav/principals/",
                    "calendars": {
                        "primary": {
                            "collection_path": "/dav/calendars/work/main/",
                            "display_name": "Work Main",
                            "role": "primary",
                            "write_policy": "dry_run_and_approval",
                            "default_timezone": "Europe/Vienna",
                        },
                        "read-shared": {
                            "collection_url": "https://cal.example.com/dav/calendars/shared/team/",
                            "display_name": "Team Shared",
                            "role": "shared",
                            "write_policy": "read_only",
                        },
                    },
                }
            },
        }
        mock_env = {
            "CALDAV_USERNAME_WORK": "user",
            "CALDAV_PASSWORD_WORK": "secret",
        }

        # Default calendar resolution
        target_default = resolve_calendar_target(
            backend_cfg, calendar_alias="primary", env=mock_env
        )
        self.assertEqual(target_default["account_id"], "work")
        self.assertEqual(target_default["calendar_alias"], "primary")
        self.assertEqual(
            target_default["collection_url"],
            "https://cal.example.com/dav/calendars/work/main/",
        )
        self.assertEqual(target_default["role"], "primary")
        self.assertEqual(target_default["write_policy"], "dry_run_and_approval")
        self.assertEqual(target_default["default_timezone"], "Europe/Vienna")
        self.assertIsInstance(target_default["credentials"], AccountCredentials)

        # Explicit calendar resolution with absolute collection_url
        target_shared = resolve_calendar_target(
            backend_cfg,
            account_id="work",
            calendar_alias="read-shared",
            env=mock_env,
        )
        self.assertEqual(
            target_shared["collection_url"],
            "https://cal.example.com/dav/calendars/shared/team/",
        )
        self.assertEqual(target_shared["role"], "shared")
        self.assertEqual(target_shared["write_policy"], "read_only")

    def test_resolve_calendar_target_not_found(self) -> None:
        backend_cfg = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {
                "work": {
                    "principal_url": "https://cal.example.com/dav/",
                    "calendars": {},
                }
            },
        }
        mock_env = {
            "CALDAV_USERNAME_WORK": "user",
            "CALDAV_PASSWORD_WORK": "secret",
        }
        with self.assertRaises(CalDAVConfigError) as ctx:
            resolve_calendar_target(
                backend_cfg, calendar_alias="nonexistent", env=mock_env
            )
        self.assertEqual(ctx.exception.code, "CALDAV_CALENDAR_NOT_FOUND")

    def test_collection_url_enforces_https_and_matching_origin(self) -> None:
        mock_env = {
            "CALDAV_USERNAME_WORK": "user",
            "CALDAV_PASSWORD_WORK": "secret",
        }
        # Insecure HTTP collection_url
        cfg_insecure = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {
                "work": {
                    "principal_url": "https://cal.example.com/dav/",
                    "calendars": {
                        "insecure": {
                            "collection_url": "http://insecure.example.com/cal",
                            "role": "shared",
                        }
                    },
                }
            },
        }
        with self.assertRaises(CalDAVConfigError) as ctx_insecure:
            resolve_calendar_target(
                cfg_insecure, calendar_alias="insecure", env=mock_env
            )
        self.assertIn(
            ctx_insecure.exception.code,
            ("CALDAV_INSECURE_URL", "CALDAV_CROSS_ORIGIN_URL", "CALDAV_CONFIG_INVALID"),
        )

        # Cross-origin collection_url (different host)
        cfg_cross = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {
                "work": {
                    "principal_url": "https://cal.example.com/dav/",
                    "calendars": {
                        "cross": {
                            "collection_url": "https://other.example.com/cal",
                            "role": "shared",
                        }
                    },
                }
            },
        }
        with self.assertRaises(CalDAVConfigError) as ctx_cross:
            resolve_calendar_target(
                cfg_cross, calendar_alias="cross", env=mock_env
            )
        self.assertIn(
            ctx_cross.exception.code,
            ("CALDAV_CROSS_ORIGIN_URL", "CALDAV_REDIRECT_FORBIDDEN", "CALDAV_CONFIG_INVALID"),
        )

        # Port mismatch
        cfg_port = {
            "schema": "calendar-desk-backend/1",
            "default_account": "work",
            "accounts": {
                "work": {
                    "principal_url": "https://cal.example.com/dav/",
                    "calendars": {
                        "port": {
                            "collection_url": "https://cal.example.com:8443/cal",
                            "role": "shared",
                        }
                    },
                }
            },
        }
        with self.assertRaises(CalDAVConfigError) as ctx_port:
            resolve_calendar_target(
                cfg_port, calendar_alias="port", env=mock_env
            )
        self.assertIn(
            ctx_port.exception.code,
            ("CALDAV_CROSS_ORIGIN_URL", "CALDAV_REDIRECT_FORBIDDEN", "CALDAV_CONFIG_INVALID"),
        )


if __name__ == "__main__":
    unittest.main()
