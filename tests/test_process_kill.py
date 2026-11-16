"""Kill a live publisher process at each sync point, then verify recovery.

Real process termination (``Popen.kill``) is used at both points:

* pre-commit  -> the transaction is open and holds the SQLite write lock when
                 the process dies. SQLite must roll it back on connection loss.
* post-commit -> the COMMIT is durable but the client never sees a response.

After each kill the test restarts service processes, reads the directory
through HTTP, and retries the original snapshot, asserting whole-snapshot
atomicity, idempotency/conflict semantics, and that the version never moves
backwards.
"""

from __future__ import annotations

import unittest

from tests.support import (
    ACME,
    ACME_IDP,
    Cluster,
    Environment,
    GatePublisher,
    account,
    http_json,
    identity,
    snapshot,
)

BASE_ACCOUNTS = [
    account("acme-1001", "ana.reyes@acme.example", "Ana Reyes", True),
    account("acme-1002", "bola.okafor@acme.example", "Bola Okafor", True),
    account("acme-1003", "shared.mailbox@acme.example", "Shared Mailbox", True),
]
BASE_IDENTITIES = [
    identity(ACME_IDP, "00u-as-9011", "acme-1001"),
    identity(ACME_IDP, "00u-bo-4412", "acme-1002"),
]

# The replacement snapshot changes every account and rebinds both identities to
# new targets, so a partially applied version would be unmistakable.
REPLACEMENT_ACCOUNTS = [
    account("acme-2001", "new.one@acme.example", "New One", True),
    account("acme-2002", "new.two@acme.example", "New Two", False),
]
REPLACEMENT_IDENTITIES = [
    identity(ACME_IDP, "00u-as-9011", "acme-2002"),
    identity(ACME_IDP, "00u-bo-4412", "acme-2001"),
]


def expected_results(identities, accounts):
    """Literal reference: (issuer, subject) -> (account_id|None, enabled|None)."""
    enabled = {a["account_id"]: a["enabled"] for a in accounts}
    return {
        (i["issuer"], i["subject"]): (i["account_id"], enabled[i["account_id"]])
        for i in identities
    }


class ProcessKillRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment(Environment.for_current_test(self),
                               f"{type(self).__name__}.{self._testMethodName}")
        self.cluster = Cluster(self.env).start(4)
        status, body = self.cluster.publish(
            0, ACME, snapshot(ACME, 10, BASE_ACCOUNTS, BASE_IDENTITIES)
        )
        self.assertEqual(status, 201, body)
        self.gates: list[GatePublisher] = []

    def tearDown(self) -> None:
        for gate in self.gates:
            if gate.proc is not None:
                try:
                    gate.proc.kill()
                    gate.proc.wait(timeout=10)
                except Exception:
                    pass
        self.cluster.stop()

    # -- helpers ---------------------------------------------------------

    def assert_directory(self, revision, accounts, identities):
        stats = self.cluster.stats(0, ACME)
        self.assertEqual(stats["revision"], revision, stats)
        self.assertEqual(stats["account_count"], len(accounts), stats)
        self.assertEqual(stats["identity_count"], len(identities), stats)

        status, body = http_json(
            self.cluster[0].base_url, "GET", f"/v1/tenants/{ACME}/bindings"
        )
        self.assertEqual(status, 200, body)
        actual = {(b["issuer"], b["subject"]): b["account_id"] for b in body["bindings"]}
        self.assertEqual(actual, {(i["issuer"], i["subject"]): i["account_id"] for i in identities})

        reference = expected_results(identities, accounts)
        for (issuer, subject), (account_id, is_enabled) in reference.items():
            status, body = self.cluster.resolve(0, ACME, "corp-idp-adapter", issuer, subject)
            if account_id is None:
                self.assertEqual(status, 403, body)
                continue
            if is_enabled:
                self.assertEqual(status, 200, body)
                self.assertEqual(body["account_id"], account_id, body)
                self.assertEqual(body["revision"], revision, body)
            else:
                self.assertEqual(status, 403, body)
                self.assertEqual(body["error"]["details"]["account_id"], account_id, body)
                self.assertEqual(body["error"]["details"]["revision"], revision, body)

    def new_publisher(self, name, revision, accounts, identities, sync_point) -> GatePublisher:
        """A publisher process that owns the transaction itself (direct DB).

        Killing it therefore kills the transaction owner, which is the crash
        the requirement targets. An HTTP-client publisher would leave the
        server alive inside the transaction, so that variant is covered
        separately in ``test_kill_service_mid_transaction``.
        """
        gate = GatePublisher(
            self.env,
            name,
            ACME,
            snapshot(ACME, revision, accounts, identities),
            None,
            sync_point,
        )
        self.gates.append(gate)
        return gate.start()

    def restart_servers(self) -> None:
        """Terminate all services and start fresh ones on the same database."""
        self.cluster.stop()
        self.cluster = Cluster(self.env).start(4)

    # -- tests -----------------------------------------------------------

    def test_kill_before_commit_leaves_directory_intact(self):
        gate = self.new_publisher(
            "prekill", 11, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES, "pre-commit"
        )
        gate.kill()
        self.env.log.write({"event": "killed_before_commit", "revision": 11})

        # Bookkeeping: the gate file survives, no result file (no response).
        self.assertTrue(gate.gate.exists())
        self.assertFalse(gate.result_path.exists())

        self.restart_servers()
        self.assert_directory(10, BASE_ACCOUNTS, BASE_IDENTITIES)

        # Retrying the original snapshot now publishes normally.
        status, body = self.cluster.publish(
            1, ACME, snapshot(ACME, 11, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)
        )
        self.assertEqual(status, 201, body)
        self.assert_directory(11, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

        # Retrying it again is idempotent, persisted across another restart.
        status, body = self.cluster.publish(
            2, ACME, snapshot(ACME, 11, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "unchanged")
        self.restart_servers()
        self.assert_directory(11, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

        # A lower revision submitted after recovery is stale, not applied.
        status, body = self.cluster.publish(
            3, ACME, snapshot(ACME, 9, BASE_ACCOUNTS, BASE_IDENTITIES)
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "stale_revision")
        self.assert_directory(11, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

    def test_kill_after_commit_keeps_durable_revision(self):
        gate = self.new_publisher(
            "postkill", 12, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES, "post-commit"
        )
        gate.kill()
        self.env.log.write({"event": "killed_after_commit", "revision": 12})
        self.assertTrue(gate.gate.exists())
        self.assertFalse(gate.result_path.exists())

        self.restart_servers()
        # The commit survived: revision 12 is fully present, not partially.
        self.assert_directory(12, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

        # Retrying the same snapshot is an idempotent success, not a conflict.
        status, body = self.cluster.publish(
            0, ACME, snapshot(ACME, 12, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "unchanged")
        self.assertEqual(body["revision"], 12)
        self.assertGreaterEqual(body["publication_count"], 2)

        # A conflicting body at the same revision is rejected.
        conflicting = snapshot(
            ACME, 12, BASE_ACCOUNTS, BASE_IDENTITIES
        )
        status, body = self.cluster.publish(1, ACME, conflicting)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "revision_conflict")

        # And an older revision never wins after restart.
        status, body = self.cluster.publish(
            2, ACME, snapshot(ACME, 10, BASE_ACCOUNTS, BASE_IDENTITIES)
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "stale_revision")
        self.assert_directory(12, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

    def test_kill_precommit_does_not_block_other_publishers(self):
        gate = self.new_publisher(
            "prekill2", 13, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES, "pre-commit"
        )
        # While the direct publisher holds the write lock, a newer revision
        # from an HTTP publisher queues behind it. Kill the holder: the waiter
        # must then acquire the lock and succeed rather than being wedged.
        import threading
        import time

        outcome: dict[str, object] = {}

        def publish_later():
            try:
                outcome["value"] = self.cluster.publish(
                    3, ACME, snapshot(ACME, 14, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)
                )
            except Exception as exc:  # pragma: no cover - connection reset
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=publish_later)
        thread.start()
        time.sleep(0.3)
        gate.kill()
        thread.join(timeout=90)
        self.assertIn("value", outcome, outcome)
        status, body = outcome["value"]  # type: ignore[misc]
        self.assertEqual(status, 201, body)

        self.restart_servers()
        self.assert_directory(14, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

        # The killed publisher's revision 13 never became visible; retrying it
        # now is stale because 14 is current.
        status, body = self.cluster.publish(
            0, ACME, snapshot(ACME, 13, BASE_ACCOUNTS, BASE_IDENTITIES)
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "stale_revision")
        self.assert_directory(14, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

    def test_kill_service_mid_transaction(self):
        """Kill a *service* process while it is inside the write transaction.

        The PUT client is blocked on the gated server; the server is killed
        for real, so the client observes a dropped connection and SQLite must
        roll the open transaction back when the process dies.
        """
        import threading
        import time

        from tests.support import ConnectionFailed, http_json

        server = self.cluster[3]
        gate_path = self.env.dir / "gate-service.flag"
        outcome: dict[str, object] = {}
        payload = snapshot(ACME, 17, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

        def publish_gated():
            headers = {
                "X-Sync-Point": "pre-commit",
                "X-Sync-Gate-B64": __import__("base64").b64encode(
                    str(gate_path).encode("utf-8")
                ).decode("ascii"),
                "X-Sync-Timeout": "60",
            }
            try:
                outcome["value"] = http_json(
                    server.base_url, "PUT", f"/v1/tenants/{ACME}/directory",
                    body=payload, headers=headers, timeout=90,
                )
            except ConnectionFailed as exc:
                outcome["connection_failed"] = str(exc)
            except Exception as exc:  # pragma: no cover - unexpected transport
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=publish_gated)
        thread.start()
        deadline = time.monotonic() + 30
        while not gate_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(gate_path.exists(), "service never reached the sync point")

        wait_status = server.kill()
        thread.join(timeout=60)
        self.assertTrue(
            "connection_failed" in outcome or "error" in outcome, outcome
        )
        self.env.log.write(
            {"event": "killed_service_mid_transaction", "wait_status": wait_status,
             "revision": 17}
        )

        self.cluster.stop()
        self.cluster = Cluster(self.env).start(4)
        self.assert_directory(10, BASE_ACCOUNTS, BASE_IDENTITIES)

        status, body = self.cluster.publish(
            0, ACME, snapshot(ACME, 17, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)
        )
        self.assertEqual(status, 201, body)
        self.assert_directory(17, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

    def test_kill_service_after_commit(self):
        """Kill a service process after COMMIT but before the HTTP response.

        The commit is already durable, so the revision must survive; only the
        client's view is lost.
        """
        import base64
        import threading
        import time

        from tests.support import ConnectionFailed, http_json

        server = self.cluster[3]
        gate_path = self.env.dir / "gate-service-post.flag"
        outcome: dict[str, object] = {}
        payload = snapshot(ACME, 18, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

        def publish_gated():
            headers = {
                "X-Sync-Point": "post-commit",
                "X-Sync-Gate-B64": base64.b64encode(
                    str(gate_path).encode("utf-8")
                ).decode("ascii"),
                "X-Sync-Timeout": "60",
            }
            try:
                outcome["value"] = http_json(
                    server.base_url, "PUT", f"/v1/tenants/{ACME}/directory",
                    body=payload, headers=headers, timeout=90,
                )
            except ConnectionFailed as exc:
                outcome["connection_failed"] = str(exc)
            except Exception as exc:  # pragma: no cover
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=publish_gated)
        thread.start()
        deadline = time.monotonic() + 30
        while not gate_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(gate_path.exists(), "service never reached the sync point")
        server.kill()
        thread.join(timeout=60)
        self.assertTrue("connection_failed" in outcome or "error" in outcome, outcome)

        self.cluster.stop()
        self.cluster = Cluster(self.env).start(4)
        self.assert_directory(18, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

        status, body = self.cluster.publish(
            0, ACME, snapshot(ACME, 18, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "unchanged")
        self.assert_directory(18, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)

    def test_both_kill_points_in_sequence(self):
        pre = self.new_publisher("seq-pre", 15, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES,
                                 "pre-commit")
        pre.kill()
        self.assert_directory(10, BASE_ACCOUNTS, BASE_IDENTITIES)

        status, body = self.cluster.publish(
            0, ACME, snapshot(ACME, 15, REPLACEMENT_ACCOUNTS, REPLACEMENT_IDENTITIES)
        )
        self.assertEqual(status, 201, body)

        post = self.new_publisher("seq-post", 16, BASE_ACCOUNTS, BASE_IDENTITIES, "post-commit")
        post.kill()
        self.restart_servers()
        self.assert_directory(16, BASE_ACCOUNTS, BASE_IDENTITIES)

        status, body = self.cluster.publish(
            1, ACME, snapshot(ACME, 16, BASE_ACCOUNTS, BASE_IDENTITIES)
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["idempotent"], True)
        self.assert_directory(16, BASE_ACCOUNTS, BASE_IDENTITIES)


if __name__ == "__main__":
    unittest.main()
