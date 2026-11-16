"""End-to-end CLI tests matching the documented Windows commands.

Also asserts that no adapter credential ever appears in CLI output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.support import (
    ACME,
    ACME_IDP,
    ACME_PARTNER,
    ROOT,
    Environment,
    account,
    identity,
    snapshot,
)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment(Environment.for_current_test(self),
                               f"{type(self).__name__}.{self._testMethodName}")

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = self.env.subprocess_env()
        result = subprocess.run(
            [sys.executable, "-m", "idsvc", *args],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        combined = result.stdout + result.stderr
        for adapter in self.env._adapters.values():
            self.assertNotIn(adapter.credential, combined,
                             "adapter credential leaked into CLI output")
        return result

    def test_init_is_idempotent_and_does_not_rotate_credentials(self):
        # Environment() already ran init once; a fresh database path is needed
        # to observe the first-write behaviour.
        db = self.env.dir / "fresh.db"
        adapters = self.env.dir / "fresh-adapters.json"
        first = self.run_cli("init", "--db", str(db), "--adapters", str(adapters))
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        self.assertTrue(payload["credentials_written"])
        original = adapters.read_text(encoding="utf-8")

        second = self.run_cli("init", "--db", str(db), "--adapters", str(adapters))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(json.loads(second.stdout)["credentials_written"])
        self.assertEqual(adapters.read_text(encoding="utf-8"), original)

    def test_publish_show_bindings_history_account_flow(self):
        payload = snapshot(
            ACME, 3,
            [account("acme-1", "ana@acme.example", "Ana", True),
             account("acme-2", "shared@acme.example", "Shared", False)],
            [identity(ACME_IDP, "s-1", "acme-1"),
             identity(ACME_PARTNER, "shared", "acme-2")],
        )
        path = self.env.write_snapshot("acme_r3", payload)

        out = self.run_cli("publish", "--db", str(self.env.db_path), "--tenant", ACME,
                           "--file", str(path))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["response"]["outcome"], "published")

        out = self.run_cli("show", "--db", str(self.env.db_path), "--tenant", ACME)
        stats = json.loads(out.stdout)
        self.assertEqual((stats["revision"], stats["account_count"], stats["identity_count"]),
                         (3, 2, 2))

        out = self.run_cli("bindings", "--db", str(self.env.db_path), "--tenant", ACME)
        bindings = json.loads(out.stdout)["bindings"]
        self.assertEqual(
            {(b["issuer"], b["subject"]): b["account_id"] for b in bindings},
            {(ACME_IDP, "s-1"): "acme-1", (ACME_PARTNER, "shared"): "acme-2"},
        )

        out = self.run_cli("account", "--db", str(self.env.db_path), "--tenant", ACME,
                           "--account-id", "acme-2")
        account_payload = json.loads(out.stdout)
        self.assertEqual(account_payload["email"], "shared@acme.example")
        self.assertEqual(account_payload["enabled"], False)
        self.assertEqual(account_payload["identities"],
                         [{"issuer": ACME_PARTNER, "subject": "shared"}])

        out = self.run_cli("history", "--db", str(self.env.db_path), "--tenant", ACME)
        self.assertEqual([r["revision"] for r in json.loads(out.stdout)["revisions"]], [3])

        out = self.run_cli("tenants", "--db", str(self.env.db_path))
        self.assertEqual(json.loads(out.stdout)["tenants"], [{"tenant": ACME, "revision": 3}])

    def test_resolve_cli_disabled_identity_exits_nonzero(self):
        payload = snapshot(
            ACME, 1,
            [account("acme-1", "ana@acme.example", "Ana", True),
             account("acme-2", "shared@acme.example", "Shared", False)],
            [identity(ACME_IDP, "s-1", "acme-1"),
             identity(ACME_IDP, "s-2", "acme-2")],
        )
        path = self.env.write_snapshot("acme_r1", payload)
        self.assertEqual(self.run_cli("publish", "--db", str(self.env.db_path),
                                      "--tenant", ACME, "--file", str(path)).returncode, 0)

        out = self.run_cli("resolve", "--db", str(self.env.db_path), "--tenant", ACME,
                           "--issuer", ACME_IDP, "--subject", "s-1",
                           "--adapters", str(self.env.adapters_path))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["account_id"], "acme-1")

        out = self.run_cli("resolve", "--db", str(self.env.db_path), "--tenant", ACME,
                           "--issuer", ACME_IDP, "--subject", "s-2",
                           "--adapters", str(self.env.adapters_path))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertEqual(json.loads(out.stdout)["error"]["code"], "identity_not_resolvable")

    def test_batch_cli(self):
        payload = snapshot(
            ACME, 2,
            [account("acme-1", "ana@acme.example", "Ana", True)],
            [identity(ACME_IDP, "s-1", "acme-1")],
        )
        self.run_cli("publish", "--db", str(self.env.db_path), "--tenant", ACME,
                     "--file", str(self.env.write_snapshot("acme_r2", payload)))
        batch = {"identities": [{"issuer": ACME_IDP, "subject": "s-1"},
                                {"issuer": ACME_IDP, "subject": "missing"}]}
        batch_path = self.env.dir / "batch.json"
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        out = self.run_cli("batch", "--db", str(self.env.db_path), "--tenant", ACME,
                           "--file", str(batch_path), "--adapters", str(self.env.adapters_path))
        self.assertEqual(out.returncode, 0, out.stderr)
        body = json.loads(out.stdout)
        self.assertEqual(body["revision"], 2)
        self.assertEqual([r["resolved"] for r in body["results"]], [True, False])

    def test_serve_requires_initialised_database(self):
        result = subprocess.run(
            [sys.executable, "-m", "idsvc", "serve",
             "--db", str(self.env.dir / "absent.db"),
             "--adapters", str(self.env.adapters_path), "--port", "0"],
            cwd=str(ROOT), env=self.env.subprocess_env(),
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialised", result.stdout + result.stderr)

    def test_publish_invalid_snapshot_reports_error_without_traceback(self):
        bad = self.env.write_snapshot("bad", {
            "revision": True, "accounts": [], "identities": []
        })
        out = self.run_cli("publish", "--db", str(self.env.db_path), "--tenant", ACME,
                           "--file", str(bad))
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("Traceback", out.stderr)
        self.assertEqual(json.loads(out.stdout)["error"]["code"], "invalid_snapshot")


if __name__ == "__main__":
    unittest.main()
