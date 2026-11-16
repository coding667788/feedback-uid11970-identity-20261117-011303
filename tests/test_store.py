"""Store-level tests without an HTTP server.

These exercise the durable guarantees directly (persistence across new
connections/processes, database-level uniqueness) so that the HTTP layer is
not the only thing standing between a bad state and the disk.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest

from idsvc.errors import RevisionConflict, RevisionTaken, StaleRevision
from idsvc.snapshot import validate_snapshot
from idsvc.store import Store

from tests.support import ACME, ACME_IDP, ROOT, Environment, account, identity, snapshot


def make_store(directory) -> Store:
    store = Store(str(directory / "store-level.db"))
    store.initialize()
    return store


def snap(revision, accounts, identities, tenant=ACME):
    return validate_snapshot(snapshot(tenant, revision, accounts, identities), tenant=tenant)


class StorePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment(Environment.for_current_test(self),
                               f"{type(self).__name__}.{self._testMethodName}")
        self.store = make_store(self.env.dir)
        self.accounts = [account("acme-1", "a@acme.example", "A", True)]
        self.identities = [identity(ACME_IDP, "s-1", "acme-1")]

    def test_publish_then_reread_in_fresh_store(self):
        self.store.publish(snap(1, self.accounts, self.identities))
        fresh = Store(str(self.env.dir / "store-level.db"))
        directory = fresh.read_current(ACME)
        self.assertEqual(directory.revision, 1)
        self.assertEqual(directory.bindings[(ACME_IDP, "s-1")], "acme-1")
        self.assertTrue(directory.accounts["acme-1"].enabled)

    def test_idempotency_persists_across_new_connections(self):
        first = self.store.publish(snap(2, self.accounts, self.identities))
        self.assertEqual(first.outcome, "published")
        other = Store(str(self.env.dir / "store-level.db"))
        second = other.publish(snap(2, self.accounts, self.identities))
        self.assertEqual(second.outcome, "unchanged")
        self.assertEqual(second.publication_count, 2)
        self.assertEqual(other.publish(snap(2, self.accounts, self.identities)).publication_count, 3)

    def test_conflict_and_stale_persist(self):
        self.store.publish(snap(3, self.accounts, self.identities))
        other = Store(str(self.env.dir / "store-level.db"))
        changed = [account("acme-1", "a@acme.example", "changed", True)]
        with self.assertRaises(RevisionConflict):
            other.publish(snap(3, changed, self.identities))
        with self.assertRaises(StaleRevision):
            other.publish(snap(1, self.accounts, self.identities))
        # still exactly revision 3, unchanged
        self.assertEqual(other.read_current(ACME).revision, 3)
        self.assertEqual(other.read_current(ACME).accounts["acme-1"].display_name, "A")

    def test_swap_never_violates_uniqueness(self):
        two = [account("acme-1", "a@acme.example", "A", True),
               account("acme-2", "b@acme.example", "B", True)]
        self.store.publish(snap(4, two, [identity(ACME_IDP, "s-1", "acme-1"),
                                         identity(ACME_IDP, "s-2", "acme-2")]))
        out = self.store.publish(snap(5, two, [identity(ACME_IDP, "s-1", "acme-2"),
                                               identity(ACME_IDP, "s-2", "acme-1")]))
        self.assertEqual(out.revision, 5)
        directory = self.store.read_current(ACME)
        self.assertEqual(directory.bindings[(ACME_IDP, "s-1")], "acme-2")
        self.assertEqual(directory.bindings[(ACME_IDP, "s-2")], "acme-1")

    def test_revision_history_and_pointer_is_consistent(self):
        for revision in (6, 7, 8):
            self.store.publish(snap(revision, self.accounts, self.identities))
        history = self.store.history(ACME)
        self.assertEqual([h["revision"] for h in history], [6, 7, 8])
        self.assertEqual(self.store.stats(ACME)["revision"], 8)

    def test_database_rejects_duplicate_binding_even_without_validation(self):
        """The UNIQUE constraint is defence in depth below the schema checks."""
        self.store.publish(snap(9, self.accounts, self.identities))
        conn = sqlite3.connect(str(self.env.dir / "store-level.db"))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO bindings(tenant, issuer, subject, revision, account_id, content_hash) "
                    "VALUES(?,?,?,?,?,?)",
                    (ACME, ACME_IDP, "s-1", 9, "someone-else", "deadbeef"),
                )
        finally:
            conn.close()

    def test_failed_write_leaves_no_partial_rows(self):
        self.store.publish(snap(10, self.accounts, self.identities))
        before = self.store.stats(ACME)
        # Construct a Snapshot directly, bypassing validation, with a
        # duplicated external identity so the bindings primary key fires
        # inside the write transaction.
        from idsvc.snapshot import Account, Identity, Snapshot

        bad = Snapshot(
            tenant=ACME,
            revision=11,
            accounts=(Account("acme-1", "a", "A", True),),
            identities=(
                Identity("iss", "same", "acme-1"),
                Identity("iss", "same", "acme-1"),
            ),
        )
        with self.assertRaises(RevisionTaken):
            self.store.publish(bad)
        after = self.store.stats(ACME)
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(self.store.current_revision(ACME), 10)

    def test_unknown_tenant_reads_raise_not_found(self):
        from idsvc.errors import NotFound

        with self.assertRaises(NotFound):
            self.store.read_current("nobody")
        self.assertIsNone(self.store.current_revision("nobody"))


class CrossProcessVisibilityTests(unittest.TestCase):
    """A child process writing, this process reading, without a server."""

    def setUp(self) -> None:
        self.env = Environment(Environment.for_current_test(self),
                               f"{type(self).__name__}.{self._testMethodName}")
        self.store = make_store(self.env.dir)

    def test_child_process_publishes_and_parent_sees_it(self):
        import json

        payload = snapshot(
            ACME, 1,
            [account("acme-1", "a@acme.example", "A", True)],
            [identity(ACME_IDP, "s-1", "acme-1")],
        )
        path = self.env.write_snapshot("child", payload)
        script = (
            "import sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "from idsvc.cli import main;"
            "raise SystemExit(main(['publish','--db',sys.argv[2],'--tenant','acme','--file',sys.argv[3]]))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(ROOT), self.store.path, str(path)],
            cwd=str(self.env.dir),
            env=self.env.subprocess_env(), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["response"]["outcome"], "published")
        self.assertEqual(self.store.read_current(ACME).revision, 1)


if __name__ == "__main__":
    unittest.main()
