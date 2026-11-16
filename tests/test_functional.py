"""HTTP functional tests: isolation, exact triples, disable/remove, swaps,
invalid rollback, idempotency/conflict, publish ordering, batch consistency.

Expected values are computed in the test from literal fixtures; no parsing or
query helper from idsvc is used to derive them.
"""

from __future__ import annotations

import json
import unittest

from tests.support import (
    ACME,
    ACME_IDP,
    ACME_PARTNER,
    GLOBEX,
    GLOBEX_IDP,
    GLOBEX_PARTNER,
    Cluster,
    Environment,
    account,
    expected_map,
    http_json,
    identity,
    snapshot,
)

# ---------------------------------------------------------------------------
# Fixtures: two tenants, two providers per tenant, a shared email, a shared
# (issuer, subject) across tenants, and accounts whose email collides.
# ---------------------------------------------------------------------------

ACME_ACCOUNTS = [
    account("acme-1001", "ana.reyes@acme.example", "Ana Reyes", True),
    account("acme-1002", "bola.okafor@acme.example", "Bola Okafor", True),
    account("acme-1003", "shared.mailbox@acme.example", "Shared Mailbox", True),
    account("acme-1004", "ana.reyes@acme.example", "Ana Reyes (duplicate email)", True),
]
ACME_IDENTITIES = [
    identity(ACME_IDP, "00u-as-9011", "acme-1001"),
    identity(ACME_PARTNER, "AS-9011", "acme-1001"),
    identity(ACME_IDP, "00u-bo-4412", "acme-1002"),
    identity(ACME_PARTNER, "shared.mailbox", "acme-1003"),
    # Same email as acme-1001 but an unrelated account, deliberately unbound
    # so a resolve must never fall back to it -- and one that *is* bound so the
    # ambiguity is real if email were used.
    identity(ACME_PARTNER, "duplicate-email-holder", "acme-1004"),
]

GLOBEX_ACCOUNTS = [
    account("globex-1001", "ana.reyes@acme.example", "Ana Reyes (guest)", True),
    account("globex-1002", "cora.novak@globex.example", "Cora Novak", True),
]
GLOBEX_IDENTITIES = [
    # identical issuer AND subject as an acme binding: must not leak across.
    identity(GLOBEX_PARTNER, "AS-9011", "globex-1001"),
    identity(GLOBEX_IDP, "00u-cn-7788", "globex-1002"),
]


def acme_snapshot(revision: int, **overrides):
    return snapshot(ACME, revision, ACME_ACCOUNTS, ACME_IDENTITIES)


class ClusterTest(unittest.TestCase):
    CLUSTER_SIZE = 4

    def setUp(self) -> None:
        self._test_started = True
        name = f"{type(self).__name__}.{self._testMethodName}"
        self.env = Environment(Environment.for_current_test(self), name)
        self.cluster = Cluster(self.env).start(self.CLUSTER_SIZE)

    def tearDown(self) -> None:
        self.cluster.stop()

    # -- helpers ---------------------------------------------------------

    def publish(self, payload, tenant=ACME, server=0):
        status, body = self.cluster.publish(server, tenant, payload)
        self.assertIn(status, (200, 201), body)
        return status, body

    def resolve_ok(self, tenant, adapter_id, issuer, subject, expected_account, expected_revision,
                   server=0):
        status, body = self.cluster.resolve(server, tenant, adapter_id, issuer, subject)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["account_id"], expected_account)
        self.assertEqual(body["enabled"], True)
        self.assertEqual(body["revision"], expected_revision)
        return body

    def resolve_denied(self, tenant, adapter_id, issuer, subject, code="identity_not_resolvable",
                       server=0):
        status, body = self.cluster.resolve(server, tenant, adapter_id, issuer, subject)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], code)
        return body


class TenantIsolationAndExactMatchTests(ClusterTest):
    def test_01_cross_tenant_and_exact_triple_semantics(self):
        self.publish(acme_snapshot(10))
        status, body = self.cluster.publish(
            1, GLOBEX, snapshot(GLOBEX, 10, GLOBEX_ACCOUNTS, GLOBEX_IDENTITIES)
        )
        self.assertEqual(status, 201, body)

        # exact triple resolves in both tenants, to their own accounts
        self.resolve_ok(ACME, "partner-idp-adapter", ACME_PARTNER, "AS-9011", "acme-1001", 10)
        # the globex adapter is provisioned for the same issuer and resolves
        # the same subject into a different tenant's account
        self.resolve_ok(GLOBEX, "globex-partner-adapter", GLOBEX_PARTNER, "AS-9011", "globex-1001", 10)

        # same email in both tenants is irrelevant; no merging
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1001", 10)
        self.resolve_ok(GLOBEX, "globex-idp-adapter", GLOBEX_IDP, "00u-cn-7788", "globex-1002", 10)

        # an identity bound only in globex is unbound in acme
        self.resolve_denied(ACME, "corp-idp-adapter", ACME_IDP, "00u-cn-7788")
        self.resolve_denied(GLOBEX, "globex-idp-adapter", GLOBEX_IDP, "00u-as-9011")

        # a credential from the other tenant cannot read this directory
        status, body = http_json(
            self.cluster[0].base_url, "POST", f"/v1/tenants/{ACME}/resolve",
            body={"issuer": ACME_IDP, "subject": "00u-as-9011"},
            token=self.env.token("globex-idp-adapter"),
        )
        self.assertEqual(status, 401, body)

        # no credential at all
        status, body = http_json(
            self.cluster[0].base_url, "POST", f"/v1/tenants/{ACME}/resolve",
            body={"issuer": ACME_IDP, "subject": "00u-as-9011"},
        )
        self.assertEqual(status, 401, body)

        # credentials are not printed in the response
        _, ok_body = self.cluster.resolve(
            0, ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011"
        )
        serialized = json.dumps(ok_body)
        self.assertNotIn(self.env.token("corp-idp-adapter"), serialized)
        self.assertNotIn("credential", serialized.lower())

    def test_02_exact_string_matching(self):
        self.publish(acme_snapshot(11))
        upper_host = ACME_IDP.replace("idp.acme", "IDP.ACME")
        self.assertNotEqual(upper_host, ACME_IDP)
        variants = [
            (ACME_IDP, "00U-AS-9011"),          # case-folded subject
            (ACME_IDP, "00u-as-9011 "),         # trailing space
            (ACME_IDP, " 00u-as-9011"),         # leading space
            (upper_host, "00u-as-9011"),        # case-folded issuer
            (ACME_IDP + "/", "00u-as-9011"),    # URL-normalised issuer
            (ACME_IDP + " ", "00u-as-9011"),    # trailing space issuer
            (ACME_IDP, "00u-as-9011\n"),        # newline suffix
            (ACME_IDP + "/.", "00u-as-9011"),   # dot-segment normalisation
        ]
        for issuer, subject in variants:
            with self.subTest(issuer=issuer, subject=subject):
                self.resolve_denied(ACME, "corp-idp-adapter", issuer, subject)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1001", 11)

    def test_03_same_email_never_merges(self):
        self.publish(acme_snapshot(12))
        # acme-1001 and acme-1004 share an email. Each binding resolves only
        # through its own triple; there is no email-based path at all.
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1001", 12)
        self.resolve_ok(ACME, "partner-idp-adapter", ACME_PARTNER, "duplicate-email-holder",
                        "acme-1004", 12)
        # an unbound subject with the shared email fails rather than matching
        self.resolve_denied(ACME, "partner-idp-adapter", ACME_PARTNER, "ana.reyes@acme.example")
        # account attributes expose both accounts independently
        status, body = http_json(
            self.cluster[0].base_url, "GET", f"/v1/tenants/{ACME}/accounts/acme-1004"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["email"], "ana.reyes@acme.example")
        self.assertEqual(body["account_id"], "acme-1004")

    def test_04_email_is_mutable_without_affecting_resolution(self):
        payload = acme_snapshot(13)
        for item in payload["accounts"]:
            if item["account_id"] == "acme-1001":
                item["email"] = "renamed@acme.example"
                item["display_name"] = "Renamed Person"
        self.publish(payload)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1001", 13)
        status, body = http_json(
            self.cluster[0].base_url, "GET", f"/v1/tenants/{ACME}/accounts/acme-1001"
        )
        self.assertEqual(body["email"], "renamed@acme.example")


class DisableAndRemoveTests(ClusterTest):
    def test_disable_then_remove_then_readd(self):
        self.publish(acme_snapshot(20))
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412", "acme-1002", 20)

        # disable acme-1002; the binding stays but login is refused
        disabled = acme_snapshot(21)
        for item in disabled["accounts"]:
            if item["account_id"] == "acme-1002":
                item["enabled"] = False
        self.publish(disabled)
        body = self.resolve_denied(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412")
        self.assertEqual(body["error"]["details"]["revision"], 21)
        self.assertEqual(body["error"]["details"]["account_id"], "acme-1002")
        self.assertEqual(body["error"]["details"]["enabled"], False)

        # remove the account and its bindings: now unbound
        removed = snapshot(ACME, 22,
                           [a for a in ACME_ACCOUNTS if a["account_id"] != "acme-1002"],
                           [i for i in ACME_IDENTITIES if i["account_id"] != "acme-1002"])
        self.publish(removed)
        body = self.resolve_denied(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412")
        self.assertEqual(body["error"]["details"], {"revision": 22})
        # everything else still works
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1001", 22)

        # a re-added account with the same account_id is a fresh row; the
        # earlier removal is not a tombstone
        self.publish(acme_snapshot(23))
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412", "acme-1002", 23)

    def test_removed_account_email_reuse_does_not_inherit_bindings(self):
        self.publish(acme_snapshot(24))
        replaced = snapshot(
            ACME,
            25,
            [account("acme-9001", "ana.reyes@acme.example", "New hire, old email", True)],
            [],
        )
        self.publish(replaced)
        self.resolve_denied(ACME, "partner-idp-adapter", ACME_PARTNER, "duplicate-email-holder")
        self.resolve_denied(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011")


class BindingSwapTests(ClusterTest):
    def test_swap_two_bindings_in_one_snapshot(self):
        self.publish(acme_snapshot(30))
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1001", 30)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412", "acme-1002", 30)

        swapped = acme_snapshot(31)
        for item in swapped["identities"]:
            if item["subject"] == "00u-as-9011":
                item["account_id"] = "acme-1002"
            elif item["subject"] == "00u-bo-4412":
                item["account_id"] = "acme-1001"
        self.publish(swapped)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1002", 31)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412", "acme-1001", 31)
        # untouched bindings are unchanged
        self.resolve_ok(ACME, "partner-idp-adapter", ACME_PARTNER, "AS-9011", "acme-1001", 31)

        # a *partial* swap that leaves a dangling reference is still rejected
        broken = acme_snapshot(32)
        for item in broken["identities"]:
            if item["subject"] == "00u-as-9011":
                item["account_id"] = "acme-1002"
        broken["accounts"] = [
            a for a in broken["accounts"] if a["account_id"] != "acme-1002"
        ]
        status, body = self.cluster.publish(0, ACME, broken)
        self.assertEqual(status, 422, body)
        self.assertEqual(self.cluster.stats(0, ACME)["revision"], 31)

    def test_retargeting_one_identity_to_duplicate_target_is_valid(self):
        # Pointing the idp binding at acme-1002, which already owns the
        # partner binding, is not a duplicate external identity: the new
        # snapshot's identity set is still unique. It must publish.
        self.publish(acme_snapshot(35))
        payload = acme_snapshot(36)
        for item in payload["identities"]:
            if item["subject"] == "00u-as-9011":
                item["account_id"] = "acme-1002"
        self.publish(payload)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1002", 36)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412", "acme-1002", 36)

    def test_swap_across_issuers(self):
        self.publish(acme_snapshot(33))
        # (partner, AS-9011) is on acme-1001; (idp, 00u-bo-4412) is on acme-1002.
        # Swap their targets in one snapshot spanning both providers.
        swapped = acme_snapshot(34)
        for item in swapped["identities"]:
            key = (item["issuer"], item["subject"])
            if key == (ACME_PARTNER, "AS-9011"):
                item["account_id"] = "acme-1002"
            elif key == (ACME_IDP, "00u-bo-4412"):
                item["account_id"] = "acme-1001"
        self.publish(swapped)
        self.resolve_ok(ACME, "partner-idp-adapter", ACME_PARTNER, "AS-9011", "acme-1002", 34)
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-bo-4412", "acme-1001", 34)
        # the other idp binding on acme-1001 is untouched
        self.resolve_ok(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011", "acme-1001", 34)


class InvalidSnapshotRollbackTests(ClusterTest):
    def _assert_unchanged(self, expected_revision: int, expected_bindings: dict):
        stats = self.cluster.stats(0, ACME)
        self.assertEqual(stats["revision"], expected_revision)
        status, body = http_json(
            self.cluster[0].base_url, "GET", f"/v1/tenants/{ACME}/bindings"
        )
        actual = {(b["issuer"], b["subject"]): b["account_id"] for b in body["bindings"]}
        self.assertEqual(actual, expected_bindings)

    def test_invalid_snapshots_leave_directory_intact(self):
        self.publish(acme_snapshot(40))
        reference = expected_map(ACME_IDENTITIES)
        self._assert_unchanged(40, reference)

        def broken(mutator, revision=41):
            payload = acme_snapshot(revision)
            mutator(payload)
            return payload

        cases = {
            "dangling binding": lambda p: p["identities"][0].__setitem__("account_id", "acme-zz"),
            "duplicate account": lambda p: p["accounts"].append(dict(p["accounts"][0])),
            "duplicate identity": lambda p: p["identities"].append(dict(p["identities"][0])),
            "duplicate identity different account": lambda p: p["identities"].append(
                {**p["identities"][0], "account_id": "acme-1002"}
            ),
            "boolean revision": lambda p: p.__setitem__("revision", True),
            "string revision": lambda p: p.__setitem__("revision", "41"),
            "string enabled": lambda p: p["accounts"][0].__setitem__("enabled", "true"),
            "number account id": lambda p: p["accounts"][0].__setitem__("account_id", 1001),
            "unknown root field": lambda p: p.__setitem__("comment", "please ignore"),
            "unknown identity field": lambda p: p["identities"][0].__setitem__("email", "x@y"),
            "missing email": lambda p: p["accounts"][0].pop("email"),
            "empty issuer": lambda p: p["identities"][0].__setitem__("issuer", ""),
        }
        for name, mutator in cases.items():
            with self.subTest(case=name):
                status, body = self.cluster.publish(1, ACME, broken(mutator))
                self.assertEqual(status, 422, body)
                self.assertEqual(body["error"]["code"], "invalid_snapshot", body)
                self._assert_unchanged(40, reference)
                self.assertNotIn("traceback", json.dumps(body).lower())

    def test_raw_json_rejections_leave_directory_intact(self):
        self.publish(acme_snapshot(45))
        reference = expected_map(ACME_IDENTITIES)
        raws = {
            "duplicate key": (
                b'{"revision": 46, "revision": 47, "accounts": [], "identities": []}'
            ),
            "nan revision": b'{"revision": NaN, "accounts": [], "identities": []}',
            "malformed": b'{"revision": 46, "accounts": [}',
            "empty body": b"",
            "not an object": b"[1,2,3]",
            "invalid utf8": b'{"revision": 46, "accounts": [], "identities": [], '
                            b'"tenant": "\xff"}',
        }
        for name, raw in raws.items():
            with self.subTest(case=name):
                status, body = self.cluster.publish_raw(2, ACME, raw)
                self.assertEqual(status, 422, body)
                self._assert_unchanged(45, reference)

    def test_wrong_tenant_in_body_rejected(self):
        self.publish(acme_snapshot(48), tenant=ACME)
        payload = acme_snapshot(49)
        payload["tenant"] = GLOBEX
        status, body = self.cluster.publish(0, ACME, payload)
        self.assertEqual(status, 422, body)
        self.assertEqual(self.cluster.stats(0, ACME)["revision"], 48)


class RevisionOrderingTests(ClusterTest):
    def test_idempotent_equivalent_snapshot(self):
        payload = acme_snapshot(50)
        status, first = self.cluster.publish(0, ACME, payload)
        self.assertEqual(status, 201, first)
        self.assertEqual(first["outcome"], "published")

        # same content, different order of both arrays and object keys
        reordered = {
            "identities": [
                {k: item[k] for k in ("account_id", "subject", "issuer")}
                for item in reversed(payload["identities"])
            ],
            "accounts": [
                {k: item[k] for k in ("enabled", "display_name", "email", "account_id")}
                for item in reversed(payload["accounts"])
            ],
            "revision": 50,
            "tenant": ACME,
        }
        status, second = self.cluster.publish(1, ACME, reordered)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(second["idempotent"], True)
        self.assertEqual(second["content_hash"], first["content_hash"])
        self.assertEqual(second["publication_count"], first["publication_count"] + 1)

        # persisted: a different process still reports the same decision
        status, third = self.cluster.publish(2, ACME, payload)
        self.assertEqual(status, 200, third)
        self.assertEqual(self.cluster.stats(3, ACME)["revision"], 50)

    def test_conflict_same_revision_different_content(self):
        self.publish(acme_snapshot(51))
        different = acme_snapshot(51)
        different["accounts"][0]["display_name"] = "Changed"
        status, body = self.cluster.publish(0, ACME, different)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "revision_conflict")
        self.assertEqual(body["error"]["details"]["revision"], 51)
        self.assertNotEqual(
            body["error"]["details"]["published_content_hash"],
            body["error"]["details"]["submitted_content_hash"],
        )
        # the original content still wins
        status, body = self.cluster.resolve(
            1, ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011"
        )
        self.assertEqual(body["revision"], 51)
        stats = self.cluster.stats(0, ACME)
        status, history = http_json(
            self.cluster[0].base_url, "GET", f"/v1/tenants/{ACME}/history"
        )
        rev51 = [r for r in history["revisions"] if r["revision"] == 51]
        self.assertEqual(len(rev51), 1)
        self.assertEqual(rev51[0]["content_hash"], stats["content_hash"])

    def test_stale_lower_revision_rejected(self):
        self.publish(acme_snapshot(52))
        self.publish(acme_snapshot(53))
        stale = acme_snapshot(52)
        stale["accounts"][0]["display_name"] = "Stale edit"
        status, body = self.cluster.publish(2, ACME, stale)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "stale_revision")
        self.assertEqual(body["error"]["details"]["current_revision"], 53)
        self.assertEqual(self.cluster.stats(0, ACME)["revision"], 53)

    def test_higher_revision_replaces_directory_completely(self):
        self.publish(acme_snapshot(54))
        self.publish(snapshot(ACME, 55, [], []))
        stats = self.cluster.stats(0, ACME)
        self.assertEqual(stats["revision"], 55)
        self.assertEqual(stats["account_count"], 0)
        self.assertEqual(stats["identity_count"], 0)
        self.resolve_denied(ACME, "corp-idp-adapter", ACME_IDP, "00u-as-9011")

    def test_revision_of_another_tenant_is_independent(self):
        self.publish(snapshot(GLOBEX, 5, GLOBEX_ACCOUNTS, GLOBEX_IDENTITIES), tenant=GLOBEX)
        self.publish(acme_snapshot(60))
        # globex revision 5 is unaffected by acme's 60 and vice versa
        status, body = self.cluster.publish(
            1, GLOBEX, snapshot(GLOBEX, 6, GLOBEX_ACCOUNTS, GLOBEX_IDENTITIES)
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(self.cluster.stats(0, ACME)["revision"], 60)
        self.assertEqual(self.cluster.stats(0, GLOBEX)["revision"], 6)


class BatchResolutionTests(ClusterTest):
    def test_batch_all_or_nothing_per_identity_and_one_revision(self):
        self.publish(acme_snapshot(70))
        requested = [
            {"issuer": ACME_IDP, "subject": "00u-as-9011"},
            {"issuer": ACME_PARTNER, "subject": "dual-listed"},
            {"issuer": ACME_PARTNER, "subject": "AS-9011"},
        ]
        status, body = self.cluster.batch(0, ACME, "corp-idp-adapter", requested)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revision"], 70)
        by_identity = {
            (r["issuer"], r["subject"]): r for r in body["results"]
        }
        self.assertEqual(by_identity[(ACME_IDP, "00u-as-9011")]["account_id"], "acme-1001")
        self.assertEqual(by_identity[(ACME_PARTNER, "AS-9011")]["account_id"], "acme-1001")
        unbound = by_identity[(ACME_PARTNER, "dual-listed")]
        self.assertFalse(unbound["resolved"])
        self.assertEqual(unbound["revision"], 70)
        self.assertEqual(body["resolved_count"], 2)
        self.assertFalse(body["all_resolved"])

    def test_batch_rejects_malformed_requests(self):
        cases = [
            {"not_identities": []},
            {"identities": {}, },
            {"identities": [{"issuer": "x"}]},
            {"identities": [{"issuer": "x", "subject": "y", "extra": 1}]},
            {"identities": [{"issuer": 1, "subject": "y"}]},
            {"identities": []},
            {"identities": [{"issuer": "", "subject": "y"}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                status, body = http_json(
                    self.cluster[0].base_url, "POST",
                    f"/v1/tenants/{ACME}/resolve-batch",
                    body=payload, token=self.env.token("corp-idp-adapter"),
                )
                self.assertEqual(status, 422, body)

    def test_batch_requires_matching_tenant_credential(self):
        self.publish(acme_snapshot(72))
        status, body = http_json(
            self.cluster[1].base_url, "POST", f"/v1/tenants/{ACME}/resolve-batch",
            body={"identities": [{"issuer": ACME_IDP, "subject": "00u-as-9011"}]},
            token=self.env.token("globex-idp-adapter"),
        )
        self.assertEqual(status, 401, body)


class ApiContractTests(ClusterTest):
    def test_unknown_routes_and_methods(self):
        status, body = http_json(self.cluster[0].base_url, "GET", "/v1/tenants/acme/nope")
        self.assertEqual(status, 404, body)
        status, body = http_json(
            self.cluster[0].base_url, "GET", f"/v1/tenants/{ACME}/directory"
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["message"], "directory publication requires PUT")
        status, body = http_json(self.cluster[0].base_url, "POST", "/v1/tenants")
        self.assertEqual(status, 404, body)

    def test_unknown_tenant_is_404(self):
        status, body = http_json(self.cluster[0].base_url, "GET", "/v1/tenants/nobody")
        self.assertEqual(status, 404, body)
        # a credential provisioned for 'acme' cannot be used to probe other
        # tenants at all, so an unknown tenant is rejected at authentication
        # before any directory lookup.
        status, body = http_json(
            self.cluster[0].base_url, "POST", "/v1/tenants/nobody/resolve",
            body={"issuer": ACME_IDP, "subject": "x"},
            token=self.env.token("corp-idp-adapter"),
        )
        self.assertEqual(status, 401, body)
        self.assertEqual(body["error"]["code"], "invalid_adapter_credentials")

    def test_health_and_tenant_listing(self):
        self.publish(acme_snapshot(80))
        status, body = http_json(self.cluster[2].base_url, "GET", "/healthz")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["schema_version"], "1")
        status, body = http_json(self.cluster[0].base_url, "GET", "/v1/tenants")
        self.assertIn({"tenant": ACME, "revision": 80}, body["tenants"])


if __name__ == "__main__":
    unittest.main()
