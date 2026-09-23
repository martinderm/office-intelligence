"""FR-22/MD-ID3 internal-domain catalog contracts (tests-only Red phase).

HEAD hardcodes the internal/external boundary of both root matchers to
``@boku.ac.at``:

* ``project_matching.select_project_match`` drops internal contacts with
  ``not c.endswith("@boku.ac.at")`` and internal domains with ``d != "boku.ac.at"``
  (project_matching.py:491-492) and reuses ``"boku.ac.at"`` inside the
  generic-freemail guard (project_matching.py:506).
* ``topic_matching.select_topic_match`` carries the same ``boku.ac.at`` literal in
  its generic domain exclusion list (topic_matching.py:727).

FR-22/MD-ID3 moves that identity-bearing boundary into the desk-signals catalog:
both owners accept an optional ``internal_domains`` keyword (``None`` -> the
documented ``DEFAULT_INTERNAL_DOMAINS`` = ``("boku.ac.at",)`` that mirrors today's
literals), and the facade ``classify_email`` threads the list it already loads from
``load_reply_heuristics`` (classifier.py:364) into both call sites (project at
classifier.py:470-480, topic at classifier.py:518-528).

MD-ID3 contract pinned by this suite (the implementation must satisfy it):

* ``select_project_match`` and ``select_topic_match`` accept an optional
  ``internal_domains`` keyword defaulting to ``None``; ``None`` resolves to
  ``DEFAULT_INTERNAL_DOMAINS`` (``("boku.ac.at",)``), byte-identical to HEAD.
* An alternative ``internal_domains`` list swaps which contact/domain counts as
  internal in both owners, exactly the way ``boku.ac.at`` is treated today (a
  swapped list swaps the internal/external outcome in both directions).
* The generic-freemail domains HEAD already excludes (gmail.com/outlook.com/
  yahoo.com) never gate a match on their own, regardless of ``internal_domains``.
* ``classify_email`` threads the catalog ``internal_domains`` into both owners,
  observable as an ``@example.org`` contact/domain gating like ``@boku.ac.at`` does
  at HEAD once the catalog declares ``internal_domains=["example.org"]``.

Scope note on the generic-freemail set: the FR-22 prose names
"gmail/outlook/yahoo/hotmail", but HEAD's project/topic guard is
``[boku.ac.at, gmail.com, outlook.com, yahoo.com]`` -- it carries ``outlook.com``
and does *not* carry ``hotmail.com`` (``hotmail.com`` belongs to the separate
``sent_indexer`` whitelist).  MD-ID3 keeps that generic list invariant ("bleibt als
generische Bundle-Konstante"), so this suite pins the HEAD-observable generic
domains gmail.com/outlook.com/yahoo.com and deliberately does not demand a new
``hotmail.com`` exclusion that would be a behaviour change outside the ticket.

All fixtures are hermetic temp directories; no mailbox, network or real catalog is
touched.  At HEAD the new keyword does not exist, so every direct call fails loud
with ``TypeError`` -- that is the genuine Red of this suite.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402
from core.matching import project_matching, reply_heuristics, topic_matching  # noqa: E402


CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "mail-desk.json"

#: The generic-freemail domains HEAD's project/topic guard excludes on top of the
#: configurable internal domain (gmail/outlook/yahoo; see the scope note above).
GENERIC_FREEMAIL_DOMAINS = ("gmail.com", "outlook.com", "yahoo.com")

#: The catalog list used to prove the alternative internal domain in both owners.
ALTERNATIVE_INTERNAL_DOMAINS = ("example.org",)


class _CatalogWorkspace:
    """Hermetic temp workspace that never touches a real catalog or mailbox."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def write_catalog(workspace_root: Path, payload: object) -> Path:
    """Write the desk-signals catalog into a hermetic workspace root."""
    path = workspace_root / CATALOG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _project(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "alpha",
        "kuerzel": "ALPHA",
        "title": "Alpha",
        "mailbox_folder": "Projects/ALPHA",
    }
    value.update(extra)
    return value


def _topic(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "alpha",
        "title": "Alpha",
        "mailbox_folder": "Themen/Alpha",
    }
    value.update(extra)
    return value


def _inputs(
    subject: str,
    *,
    from_str: str = "sender@other.test",
    to_str: str = "desk@other.test",
    cc_str: str = "",
    preview: str = "",
) -> dict[str, str]:
    """Reproduce the classifier's derived inputs for a direct owner call."""
    full_text = f"{subject}\n{from_str}\n{to_str}\n{cc_str}\n{preview}"
    return {
        "subject": subject,
        "full_text": full_text,
        "full_text_lower": full_text.lower(),
        "from_str": from_str,
        "to_str": to_str,
        "cc_str": cc_str,
        "parties": f"{from_str} {to_str} {cc_str}".lower(),
    }


def _select_project(
    projects: list[dict[str, object]], *, internal_domains: object, **inputs: str
) -> dict[str, object] | None:
    """Call the project owner with the new catalog keyword always supplied."""
    return project_matching.select_project_match(
        projects, internal_domains=internal_domains, **inputs
    )


def _select_topic(
    topics: list[dict[str, object]], *, internal_domains: object, **inputs: str
) -> dict[str, object] | None:
    """Call the topic owner with the new catalog keyword always supplied."""
    return topic_matching.select_topic_match(
        topics, internal_domains=internal_domains, **inputs
    )


class InternalDomainMatchingTests(unittest.TestCase):
    """Both root matchers consume the catalog internal-domain list."""

    def test_selectors_accept_optional_internal_domains_keyword(self) -> None:
        for name, owner in (
            ("select_project_match", project_matching.select_project_match),
            ("select_topic_match", topic_matching.select_topic_match),
        ):
            with self.subTest(function=name):
                params = inspect.signature(owner).parameters
                self.assertIn(
                    "internal_domains",
                    params,
                    f"{name} must accept the catalog internal-domain list",
                )
                self.assertIsNone(
                    params["internal_domains"].default,
                    f"{name} internal_domains must default to None (documented "
                    f"default mirrors HEAD)",
                )

    def test_project_default_internal_domains_none_keeps_boku_contact_internal(
        self,
    ) -> None:
        self.assertEqual(
            ("boku.ac.at",),
            tuple(reply_heuristics.DEFAULT_INTERNAL_DOMAINS),
            "MD-ID1 must keep the documented default internal domain",
        )
        boku_contact = _project(contacts=[{"email": "coord@boku.ac.at"}])
        inputs = _inputs("Just a note", from_str="Coord <coord@boku.ac.at>")

        implicit = project_matching.select_project_match([boku_contact], **inputs)
        explicit = _select_project(
            [boku_contact], internal_domains=None, **inputs
        )

        self.assertIsNone(
            implicit,
            "HEAD filters @boku.ac.at contacts as internal, so an internal-only "
            "party must not satisfy the external-contact-match branch",
        )
        self.assertEqual(
            explicit,
            implicit,
            "internal_domains=None must be byte-identical to HEAD",
        )

    def test_project_alternative_internal_domain_swaps_the_internal_boundary(
        self,
    ) -> None:
        example_contact = _project(contacts=[{"email": "coord@example.org"}])
        inputs = _inputs("Just a note", from_str="Coord <coord@example.org>")

        default = _select_project([example_contact], internal_domains=None, **inputs)
        overridden = _select_project(
            [example_contact],
            internal_domains=ALTERNATIVE_INTERNAL_DOMAINS,
            **inputs,
        )

        self.assertIsNotNone(
            default,
            "example.org is external under the default internal_domains, so its "
            "contact must gate the project match",
        )
        self.assertEqual("alpha", default["id"])
        self.assertIsNone(
            overridden,
            "once the catalog declares example.org internal, its contact must be "
            "filtered exactly like @boku.ac.at is today",
        )

        boku_contact = _project(contacts=[{"email": "coord@boku.ac.at"}])
        boku_inputs = _inputs("Just a note", from_str="Coord <coord@boku.ac.at>")

        self.assertIsNone(
            _select_project([boku_contact], internal_domains=None, **boku_inputs),
            "boku.ac.at stays internal under the default",
        )
        swapped = _select_project(
            [boku_contact],
            internal_domains=ALTERNATIVE_INTERNAL_DOMAINS,
            **boku_inputs,
        )
        self.assertIsNotNone(
            swapped,
            "boku.ac.at is external once it leaves internal_domains, so its "
            "contact must gate the project match",
        )
        self.assertEqual("alpha", swapped["id"])

    def test_topic_default_internal_domains_none_keeps_boku_domain_excluded(
        self,
    ) -> None:
        boku_topic = _topic(keywords=["retention"], domains=["boku.ac.at"])
        inputs = _inputs(
            "Just a note",
            from_str="sender@other.test",
            to_str="desk@boku.ac.at",
            preview="retention planning",
        )

        implicit = topic_matching.select_topic_match([boku_topic], **inputs)
        explicit = _select_topic([boku_topic], internal_domains=None, **inputs)

        self.assertIsNone(
            implicit,
            "HEAD excludes boku.ac.at from the generic domain gate, so a boku-only "
            "party plus a body keyword must not select the topic",
        )
        self.assertEqual(
            explicit,
            implicit,
            "internal_domains=None must be byte-identical to HEAD",
        )

    def test_topic_alternative_internal_domain_swaps_the_domain_exclusion(self) -> None:
        example_topic = _topic(keywords=["retention"], domains=["example.org"])
        inputs = _inputs(
            "Just a note",
            from_str="sender@other.test",
            to_str="desk@example.org",
            preview="retention planning",
        )

        default = _select_topic([example_topic], internal_domains=None, **inputs)
        overridden = _select_topic(
            [example_topic],
            internal_domains=ALTERNATIVE_INTERNAL_DOMAINS,
            **inputs,
        )

        self.assertIsNotNone(
            default,
            "example.org is external under the default, so its domain must satisfy "
            "has_domain together with the body keyword",
        )
        self.assertEqual("alpha", default["id"])
        self.assertIsNone(
            overridden,
            "once the catalog declares example.org internal, its domain must be "
            "excluded exactly like boku.ac.at is today",
        )

        boku_topic = _topic(keywords=["retention"], domains=["boku.ac.at"])
        boku_inputs = _inputs(
            "Just a note",
            from_str="sender@other.test",
            to_str="desk@boku.ac.at",
            preview="retention planning",
        )

        self.assertIsNone(
            _select_topic([boku_topic], internal_domains=None, **boku_inputs),
            "boku.ac.at stays excluded under the default",
        )
        swapped = _select_topic(
            [boku_topic],
            internal_domains=ALTERNATIVE_INTERNAL_DOMAINS,
            **boku_inputs,
        )
        self.assertIsNotNone(
            swapped,
            "boku.ac.at stops being internal once it leaves internal_domains, so its "
            "domain must gate the topic match",
        )
        self.assertEqual("alpha", swapped["id"])

    def test_project_generic_freemail_domains_never_gate_on_their_own(self) -> None:
        for domain in GENERIC_FREEMAIL_DOMAINS:
            for internal_domains in (None, ALTERNATIVE_INTERNAL_DOMAINS, ()):
                with self.subTest(domain=domain, internal_domains=internal_domains):
                    project = _project(domains=[domain])
                    inputs = _inputs("Just a note", from_str=f"x@{domain}")
                    result = _select_project(
                        [project], internal_domains=internal_domains, **inputs
                    )
                    self.assertIsNone(
                        result,
                        f"the generic freemail domain {domain} must stay excluded "
                        f"from the project domain gate regardless of internal_domains",
                    )

    def test_topic_generic_freemail_domains_never_gate_on_their_own(self) -> None:
        for domain in GENERIC_FREEMAIL_DOMAINS:
            for internal_domains in (None, ALTERNATIVE_INTERNAL_DOMAINS, ()):
                with self.subTest(domain=domain, internal_domains=internal_domains):
                    topic = _topic(keywords=["retention"], domains=[domain])
                    inputs = _inputs(
                        "Just a note",
                        from_str="sender@other.test",
                        to_str=f"desk@{domain}",
                        preview="retention planning",
                    )
                    result = _select_topic(
                        [topic], internal_domains=internal_domains, **inputs
                    )
                    self.assertIsNone(
                        result,
                        f"the generic freemail domain {domain} must stay excluded "
                        f"from the topic domain gate regardless of internal_domains",
                    )


class ClassifyEmailInternalDomainIntegrationTests(unittest.TestCase):
    """The facade threads the loaded catalog list into both root matchers."""

    def _classify(
        self,
        email: dict[str, object],
        *,
        workspace: Path,
        projects: list[dict[str, object]] | None = None,
        topics: list[dict[str, object]] | None = None,
    ) -> dict:
        return classifier.classify_email(
            email,
            workspace_root=workspace,
            projects=projects if projects is not None else [],
            topics=topics if topics is not None else [],
            sent_lookup={},
            final_index={"items": {}},
        )

    def _email(
        self,
        *,
        from_: str,
        to: str,
        subject: str = "Just a note",
        preview: str = "",
    ) -> dict[str, object]:
        return {
            "envelope_id": "md-id3",
            "folder": "INBOX",
            "message_id": "<md-id3@example.test>",
            "subject": subject,
            "from": from_,
            "to": to,
            "date": "2026-09-23",
            "preview": preview,
        }

    def _internal_domain_catalog(self, *domains: str) -> dict[str, object]:
        return {
            "schema_version": 2,
            "reply_heuristics": {
                "owner_address": "desk@example.org",
                "internal_domains": list(domains),
            },
        }

    def test_catalog_internal_domains_threads_into_project_selector(self) -> None:
        project = _project(contacts=[{"email": "coord@example.org"}])
        email = self._email(
            from_="Coordinator <coord@example.org>", to="desk@boku.ac.at"
        )

        with _CatalogWorkspace() as default_ws:
            default_item = self._classify(email, workspace=default_ws, projects=[project])
        with _CatalogWorkspace() as catalog_ws:
            write_catalog(
                catalog_ws, self._internal_domain_catalog(*ALTERNATIVE_INTERNAL_DOMAINS)
            )
            catalog_item = self._classify(email, workspace=catalog_ws, projects=[project])

        self.assertEqual(
            "project",
            default_item["decision"]["kind"],
            "without a catalog, an @example.org contact stays external and must "
            "gate the project match",
        )
        self.assertEqual("alpha", default_item["decision"]["id"])
        self.assertNotEqual(
            "project",
            catalog_item["decision"]["kind"],
            "the catalog internal_domains list must reach select_project_match so "
            "the @example.org contact is filtered like @boku.ac.at is today",
        )
        self.assertEqual("INBOX", catalog_item["action"]["target_folder"])

    def test_catalog_internal_domains_threads_into_topic_selector(self) -> None:
        topic = _topic(keywords=["retention"], domains=["example.org"])
        email = self._email(
            from_="sender@other.test",
            to="desk@example.org",
            preview="retention planning",
        )

        with _CatalogWorkspace() as default_ws:
            default_item = self._classify(email, workspace=default_ws, topics=[topic])
        with _CatalogWorkspace() as catalog_ws:
            write_catalog(
                catalog_ws, self._internal_domain_catalog(*ALTERNATIVE_INTERNAL_DOMAINS)
            )
            catalog_item = self._classify(email, workspace=catalog_ws, topics=[topic])

        self.assertEqual(
            "topic",
            default_item["decision"]["kind"],
            "without a catalog, an @example.org domain stays external and must gate "
            "the topic match",
        )
        self.assertEqual("alpha", default_item["decision"]["id"])
        self.assertNotEqual(
            "topic",
            catalog_item["decision"]["kind"],
            "the catalog internal_domains list must reach select_topic_match so the "
            "@example.org domain is excluded like boku.ac.at is today",
        )
        self.assertEqual("INBOX", catalog_item["action"]["target_folder"])


if __name__ == "__main__":
    unittest.main()
