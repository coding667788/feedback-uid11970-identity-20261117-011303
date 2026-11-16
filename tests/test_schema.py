"""Schema validation and canonical-equivalence tests (no server needed).

Expectations here are literal: they state what the documented schema accepts
or rejects, independently of how idsvc implements it.
"""

from __future__ import annotations

import unittest

from idsvc.errors import ValidationError
from idsvc.snapshot import loads_strict, validate_snapshot

from tests.support import ACME, ACME_IDP, ACME_PARTNER, account, identity, snapshot

TENANT = ACME


def valid_payload(**overrides):
    payload = snapshot(
        TENANT,
        4,
        [
            account("acme-1001", "ana@acme.example", "Ana", True),
            account("acme-1002", "bola@acme.example", "Bola", True),
            account("acme-1003", "shared@acme.example", "Shared", True),
        ],
        [
            identity(ACME_IDP, "u-1", "acme-1001"),
            identity(ACME_PARTNER, "u-1", "acme-1002"),
        ],
    )
    payload.update(overrides)
    return payload


class SchemaAcceptanceTests(unittest.TestCase):
    def test_minimal_valid_snapshot(self):
        parsed = validate_snapshot(
            {
                "revision": 1,
                "accounts": [account("a", "e@example", "n", True)],
                "identities": [identity("i", "s", "a")],
            },
            tenant="t",
        )
        self.assertEqual(parsed.revision, 1)
        self.assertEqual([a.account_id for a in parsed.accounts], ["a"])

    def test_empty_directory_is_valid(self):
        parsed = validate_snapshot({"revision": 2, "accounts": [], "identities": []}, tenant="t")
        self.assertEqual(parsed.accounts, ())
        self.assertEqual(parsed.identities, ())

    def test_identity_list_alias_keys(self):
        for key in ("identities", "bindings", "external_identities"):
            payload = valid_payload()
            payload.pop("identities")
            payload[key] = [identity(ACME_IDP, "u-1", "acme-1001")]
            parsed = validate_snapshot(payload, tenant=TENANT)
            self.assertEqual(len(parsed.identities), 1, key)

    def test_two_identity_list_keys_rejected(self):
        payload = valid_payload()
        payload["bindings"] = []
        with self.assertRaises(ValidationError) as ctx:
            validate_snapshot(payload, tenant=TENANT)
        self.assertIn("exactly one", ctx.exception.message)

    def test_tenant_optional_but_must_match_url(self):
        payload = valid_payload()
        payload.pop("tenant")
        validate_snapshot(payload, tenant=TENANT)
        payload["tenant"] = "other"
        with self.assertRaises(ValidationError) as ctx:
            validate_snapshot(payload, tenant=TENANT)
        self.assertIn("does not match", ctx.exception.message)

    def test_account_order_and_identity_order_are_irrelevant_to_canonical_form(self):
        forward = validate_snapshot(valid_payload(), tenant=TENANT)
        payload = valid_payload()
        payload["accounts"] = list(reversed(payload["accounts"]))
        payload["identities"] = list(reversed(payload["identities"]))
        reversed_snapshot = validate_snapshot(payload, tenant=TENANT)
        self.assertEqual(forward.content_hash(), reversed_snapshot.content_hash())
        self.assertEqual(forward.canonical_json(), reversed_snapshot.canonical_json())

    def test_object_key_order_is_irrelevant(self):
        payload = valid_payload()
        reordered = {
            "identities": payload["identities"],
            "revision": payload["revision"],
            "accounts": [
                {"enabled": a["enabled"], "display_name": a["display_name"],
                 "email": a["email"], "account_id": a["account_id"]}
                for a in payload["accounts"]
            ],
            "tenant": payload["tenant"],
        }
        a = validate_snapshot(payload, tenant=TENANT)
        b = validate_snapshot(reordered, tenant=TENANT)
        self.assertEqual(a.content_hash(), b.content_hash())

    def test_equivalent_but_every_field_different(self):
        payload = valid_payload()
        changed = valid_payload()
        changed["accounts"][0]["display_name"] = "Ana R."
        changed["accounts"][1]["enabled"] = False
        changed["identities"][0]["account_id"] = "acme-1003"
        self.assertNotEqual(
            validate_snapshot(payload, tenant=TENANT).content_hash(),
            validate_snapshot(changed, tenant=TENANT).content_hash(),
        )

    def test_legal_swap_is_a_valid_single_snapshot(self):
        payload = snapshot(
            TENANT,
            9,
            [
                account("acme-2001", "x@acme.example", "X", True),
                account("acme-2002", "y@acme.example", "Y", True),
            ],
            [
                identity(ACME_IDP, "swap-1", "acme-2002"),
                identity(ACME_PARTNER, "swap-2", "acme-2001"),
            ],
        )
        parsed = validate_snapshot(payload, tenant=TENANT)
        by_key = {(i.issuer, i.subject): i.account_id for i in parsed.identities}
        self.assertEqual(by_key[(ACME_IDP, "swap-1")], "acme-2002")
        self.assertEqual(by_key[(ACME_PARTNER, "swap-2")], "acme-2001")

    def test_same_email_on_two_accounts_allowed(self):
        payload = snapshot(
            TENANT,
            3,
            [
                account("acme-3001", "same@acme.example", "One", True),
                account("acme-3002", "same@acme.example", "Two", True),
            ],
            [],
        )
        parsed = validate_snapshot(payload, tenant=TENANT)
        self.assertEqual(len(parsed.accounts), 2)

    def test_nul_and_control_characters_preserved(self):
        payload = snapshot(
            TENANT,
            1,
            [account("a", "e@example", "n", True)],
            [identity("iss\u0000uer", "sub\nject\t", "a")],
        )
        parsed = validate_snapshot(payload, tenant=TENANT)
        self.assertEqual(parsed.identities[0].issuer, "iss\u0000uer")
        self.assertEqual(parsed.identities[0].subject, "sub\nject\t")

    def test_unicode_is_not_normalised(self):
        composed = "é"  # é
        decomposed = "é"
        payload = snapshot(
            TENANT, 1, [account("a", composed, "N", True)],
            [identity("i", decomposed, "a")],
        )
        parsed = validate_snapshot(payload, tenant=TENANT)
        self.assertEqual(parsed.accounts[0].email, composed)
        self.assertEqual(parsed.identities[0].subject, decomposed)


class SchemaRejectionTests(unittest.TestCase):
    def assert_rejected(self, payload, needle: str, *, tenant: str = TENANT) -> None:
        with self.assertRaises(ValidationError) as ctx:
            validate_snapshot(payload, tenant=tenant)
        self.assertIn(needle, ctx.exception.message)

    # revision typing ---------------------------------------------------

    def test_boolean_revision_rejected(self):
        self.assert_rejected(valid_payload(revision=True), "revision")

    def test_string_revision_rejected(self):
        self.assert_rejected(valid_payload(revision="4"), "revision")

    def test_float_revision_rejected(self):
        self.assert_rejected(valid_payload(revision=4.0), "revision")

    def test_null_and_zero_and_negative_revision_rejected(self):
        for bad in (None, 0, -3):
            self.assert_rejected(valid_payload(revision=bad), "revision")

    def test_missing_revision_rejected(self):
        payload = valid_payload()
        payload.pop("revision")
        self.assert_rejected(payload, "revision")

    # types -------------------------------------------------------------

    def test_enabled_must_be_boolean(self):
        for bad in (1, 0, "true", None):
            payload = valid_payload()
            payload["accounts"][0]["enabled"] = bad
            self.assert_rejected(payload, "enabled")

    def test_account_fields_must_be_strings(self):
        for field in ("account_id", "email", "display_name"):
            payload = valid_payload()
            payload["accounts"][0][field] = 123
            self.assert_rejected(payload, field)

    def test_identity_fields_must_be_strings(self):
        for field in ("issuer", "subject", "account_id"):
            payload = valid_payload()
            payload["identities"][0][field] = ["x"]
            self.assert_rejected(payload, field)

    def test_empty_identity_parts_rejected(self):
        for field in ("issuer", "subject", "account_id"):
            payload = valid_payload()
            payload["identities"][0][field] = ""
            self.assert_rejected(payload, field)

    def test_empty_account_id_rejected_email_may_be_empty(self):
        payload = valid_payload()
        payload["accounts"][0]["account_id"] = ""
        self.assert_rejected(payload, "account_id")
        payload = valid_payload()
        payload["accounts"][0]["email"] = ""
        validate_snapshot(payload, tenant=TENANT)

    # structure ---------------------------------------------------------

    def test_unknown_root_field_rejected(self):
        self.assert_rejected(valid_payload(extra=True), "unknown field")

    def test_unknown_account_field_rejected(self):
        payload = valid_payload()
        payload["accounts"][0]["nickname"] = "x"
        self.assert_rejected(payload, "unknown field")

    def test_unknown_identity_field_rejected(self):
        payload = valid_payload()
        payload["identities"][0]["email"] = "x@example"
        self.assert_rejected(payload, "unknown field")

    def test_missing_account_field_rejected(self):
        payload = valid_payload()
        payload["accounts"][0].pop("email")
        self.assert_rejected(payload, "missing")

    def test_missing_identity_list_rejected(self):
        payload = valid_payload()
        payload.pop("identities")
        self.assert_rejected(payload, "missing")

    def test_accounts_must_be_array(self):
        self.assert_rejected(valid_payload(accounts={}), "array")

    def test_root_must_be_object(self):
        self.assert_rejected([1, 2, 3], "object")

    # set rules ---------------------------------------------------------

    def test_duplicate_account_id_rejected(self):
        payload = valid_payload()
        payload["accounts"][1]["account_id"] = payload["accounts"][0]["account_id"]
        self.assert_rejected(payload, "duplicate account_id")

    def test_duplicate_external_identity_rejected_even_with_same_account(self):
        payload = valid_payload()
        payload["identities"].append(dict(payload["identities"][0]))
        self.assert_rejected(payload, "duplicate external identity")

    def test_dangling_binding_rejected(self):
        payload = valid_payload()
        payload["identities"][0]["account_id"] = "acme-9999"
        self.assert_rejected(payload, "not present")

    def test_issuer_case_difference_is_a_different_identity(self):
        payload = valid_payload()
        payload["identities"] = [
            identity("https://idp.acme.example/oidc", "u-1", "acme-1001"),
            identity("https://IDP.acme.example/oidc", "u-1", "acme-1002"),
        ]
        validate_snapshot(payload, tenant=TENANT)  # accepted: distinct identities

    def test_subject_whitespace_difference_is_a_different_identity(self):
        payload = valid_payload()
        payload["identities"] = [
            identity(ACME_IDP, "u-1", "acme-1001"),
            identity(ACME_IDP, "u-1 ", "acme-1002"),
            identity(ACME_IDP, " u-1", "acme-1003"),
        ]
        validate_snapshot(payload, tenant=TENANT)

    def test_duplicate_binding_across_accounts_rejected(self):
        payload = valid_payload()
        payload["identities"][1] = dict(payload["identities"][0])
        payload["identities"][1]["account_id"] = "acme-1002"
        self.assert_rejected(payload, "duplicate external identity")


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_object_keys_rejected(self):
        raw = '{"revision": 1, "revision": 2, "accounts": [], "identities": []}'
        with self.assertRaises(ValidationError) as ctx:
            loads_strict(raw)
        self.assertIn("duplicate object key", ctx.exception.message)

    def test_nan_and_infinity_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValidationError):
                loads_strict('{"revision": ' + constant + ', "accounts": [], "identities": []}')

    def test_malformed_json_rejected(self):
        with self.assertRaises(ValidationError):
            loads_strict('{"revision": 1,')

    def test_invalid_utf8_rejected(self):
        with self.assertRaises(ValidationError):
            loads_strict(b'{"revision": 1, "accounts": [], "identities": [], "x": "\xff"}')

    def test_unicode_escapes_are_decoded_not_interpreted(self):
        value = loads_strict('{"subject": "a\\u0000b\\u00e9"}')
        self.assertEqual(value["subject"], "a\u0000bé")

    def test_unpaired_surrogate_rejected_at_schema_level(self):
        payload = valid_payload()
        payload["identities"][0]["subject"] = "\ud800"
        with self.assertRaises(ValidationError) as ctx:
            validate_snapshot(payload, tenant=TENANT)
        self.assertIn("surrogate", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
