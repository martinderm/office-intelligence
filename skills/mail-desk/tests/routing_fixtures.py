"""Hermetic BOKU-analog catalog fixtures for the FR-17 / MD-R1 routing tests.

The builders assemble plain in-memory catalog dicts in the schema-v3 shape the
routing predicates read (``routing_priority``, ``do_not_route_if``, contacts,
keywords, subtopics).  The module reads no catalog file, opens no mailbox and
touches no network; every value is deterministic so the behavior tests can pin an
exact catalog order.

``atael`` carries the exact ``ATAEL`` subject code, ``usage-ng`` only a TUM
contact, and the BOKU-analog order deliberately places ``usage-ng`` before
``atael`` to reproduce B-1.
"""

from __future__ import annotations

from typing import Any

#: Deterministic routing targets, party addresses and subjects used by the analogs.
NEWSLETTER_FOLDER = "Newsletter"
TUM_CONTACT_EMAIL = "sybille.michaelis@tum.de"
SHARED_CONTACT_EMAIL = "shared.contact@example.test"
NO_REPLY_SENDER = "EC-NO-REPLY-GRANT-MANAGEMENT <EC-NO-REPLY-GRANT-MANAGEMENT@ec.europa.eu>"
ATAEL_SUBJECT = "WG: For Action - ATAEL - 101323118 - GAP-101323118 - Evaluation results FYI"
ATAEL_INFO_SUBJECT = "For Information - ATAEL - 581 - Grant Management"
NEWSLETTER_SUBJECT = "OeAD / Hochschule International Newsletter 7/2026"


def project_atael(**overrides: Any) -> dict[str, Any]:
    """Return the atael analog (exact kuerzel ATAEL, routing_priority 65)."""
    value: dict[str, Any] = {
        "id": "atael",
        "kuerzel": "ATAEL",
        "title": "ATAEL",
        "mailbox_folder": "Projekte/In Ausarbeitung/ATAEL",
        "aliases": [],
        "keywords": [],
        "domains": [],
        "contacts": [],
        "typical_subject_patterns": [],
        "routing_priority": 65,
    }
    value.update(overrides)
    return value


def project_usage_ng(**overrides: Any) -> dict[str, Any]:
    """Return the usage-ng analog (TUM contact only, no subject code, priority 50)."""
    value: dict[str, Any] = {
        "id": "usage-ng",
        "kuerzel": "USAGE-NG",
        "title": "USAGE-NG",
        "mailbox_folder": "Projekte/In Ausarbeitung/USAGE-NG",
        "aliases": ["usage-ng"],
        "keywords": [],
        "domains": [],
        "contacts": [{"name": "Sybille Michaelis", "email": TUM_CONTACT_EMAIL}],
        "typical_subject_patterns": [],
        "routing_priority": 50,
    }
    value.update(overrides)
    return value


def project_orion(**overrides: Any) -> dict[str, Any]:
    """Return the orion analog used by the pre-existing catalog-order expectation."""
    value: dict[str, Any] = {
        "id": "orion",
        "kuerzel": "ORION",
        "title": "ORION",
        "mailbox_folder": "Projekte/ORION",
        "aliases": [],
        "keywords": [],
        "domains": [],
        "contacts": [],
        "typical_subject_patterns": [],
        "routing_priority": 65,
    }
    value.update(overrides)
    return value


def project_contact(
    identifier: str,
    *,
    routing_priority: Any = 50,
    email: str = SHARED_CONTACT_EMAIL,
    **overrides: Any,
) -> dict[str, Any]:
    """Return a project that can match only through one shared external contact."""
    kuerzel = identifier.upper().replace("-", "")
    value: dict[str, Any] = {
        "id": identifier,
        "kuerzel": kuerzel,
        "title": kuerzel,
        "mailbox_folder": f"Projekte/{kuerzel}",
        "aliases": [],
        "keywords": [],
        "domains": [],
        "contacts": [{"email": email}],
        "typical_subject_patterns": [],
        "routing_priority": routing_priority,
    }
    value.update(overrides)
    return value


def project_contact_without_priority(
    identifier: str,
    *,
    email: str = SHARED_CONTACT_EMAIL,
    **overrides: Any,
) -> dict[str, Any]:
    """Return a contact-only project whose catalog entry omits ``routing_priority``."""
    value = project_contact(identifier, email=email, **overrides)
    value.pop("routing_priority", None)
    return value


def subtopic_eu_projekte_und_oead(**overrides: Any) -> dict[str, Any]:
    """Return the OeAD subtopic whose subject pattern identifies the netzwerke parent."""
    value: dict[str, Any] = {
        "id": "eu-projekte-und-oead",
        "title": "EU-Projekte und OeAD",
        "aliases": [],
        "keywords": ["OeAD"],
        "typical_subject_patterns": ["Hochschule International"],
        "contacts": [],
        "status": "active",
    }
    value.update(overrides)
    return value


def topic_netzwerke(**overrides: Any) -> dict[str, Any]:
    """Return the netzwerke analog (priority 60, newsletter and no-reply exclusions)."""
    value: dict[str, Any] = {
        "id": "netzwerke",
        "title": "Netzwerke",
        "mailbox_folder": "Themen/Netzwerke",
        "aliases": ["Networks"],
        "keywords": ["OeAD"],
        "typical_subject_patterns": [],
        "domains": [],
        "contacts": [],
        "routing_priority": 60,
        "do_not_route_if": ["newsletter", "no-reply"],
        "subtopics": [subtopic_eu_projekte_und_oead()],
    }
    value.update(overrides)
    return value


def topic_newsletter_only(**overrides: Any) -> dict[str, Any]:
    """Return a topic whose only exclusion is the newsletter predicate."""
    value: dict[str, Any] = {
        "id": "oead-international",
        "title": "OeAD International",
        "mailbox_folder": "Themen/OeAD International",
        "aliases": [],
        "keywords": ["OeAD"],
        "typical_subject_patterns": [],
        "domains": [],
        "contacts": [],
        "routing_priority": 50,
        "do_not_route_if": ["newsletter"],
        "subtopics": [],
    }
    value.update(overrides)
    return value


def topic_hochschule_international(**overrides: Any) -> dict[str, Any]:
    """Return a non-suppressed topic that carries a strong exact subject title."""
    value: dict[str, Any] = {
        "id": "hochschule-international",
        "title": "Hochschule International",
        "mailbox_folder": "Themen/Hochschule International",
        "aliases": ["Hochschule International"],
        "keywords": [],
        "typical_subject_patterns": [],
        "domains": [],
        "contacts": [],
        "routing_priority": 55,
        "do_not_route_if": [],
        "subtopics": [],
    }
    value.update(overrides)
    return value


def boku_analog_projects() -> list[dict[str, Any]]:
    """Return the B-1 catalog order in which usage-ng precedes atael."""
    return [project_usage_ng(), project_atael()]
