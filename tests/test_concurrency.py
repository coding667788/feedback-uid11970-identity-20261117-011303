"""Multi-process concurrency tests using real processes and sync points.

Each service is a separate OS process; each publisher is a separate OS process
that can be paused at a documented sync point (holding the SQLite write lock
at pre-commit, or already committed at post-commit). Expectations are literal
values computed in the test, not from idsvc query code.
"""

from __future__ import annotations

import threading
import time
import unittest

from tests.support import (
    ACME,
    ACME_IDP,
    ACME_PARTNER,
    Cluster,
    Environment,
    GatePublisher,
    account,
    http_json,
    identity,
    snapshot,
)

# Five accounts, five bindings: enough to make a half-applied directory
# obvious if one ever occurred.
ACCOUNTS_ENABLED = [
    account(f"acme-{n}", f"user{n}@acme.example", f"User {n}", True)
    for n in (1, 2, 3, 4, 5)
]
BINDINGS_ENABLED = [
    identity(ACME_IDP, f"sub-{n}", f"acme-{n}") for n in (1, 2, 3, 4, 5)
]
ACCOUNTS_DISABLED = [
    account(f"acme-{n}", f"user{n}@acme.example", f"User {n}", False)
    for n in (1, 2, 3, 4, 5)
]
BATCH_IDENTITIES = [{"issuer": ACME_IDP, "subject": f"sub-{n}"} for n in (1, 2, 3, 4, 5)]


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment(Environment.for_current_test(self),
                               f"{type(self).__name__}.{self._testMethodName}")
        self.cluster = Cluster(self.env).start(4)
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

    def publisher(self, name, revision, *, enabled=True, identities=None, sync_point,
                  server=1, tenant=ACME):
        payload = snapshot(
            tenant,
            revision,
            ACCOUNTS_ENABLED if enabled else ACCOUNTS_DISABLED,
            BINDINGS_ENABLED if identities is None else identities,
        )
        gate = GatePublisher(
            self.env, name, tenant, payload, self.cluster[server].base_url, sync_point
        )
        self.gates.append(gate)
        return gate.start()

    def assert_revision(self, expected: int, server=0, tenant=ACME):
        stats = self.cluster.stats(server, tenant)
        self.assertEqual(stats["revision"], expected, stats)
        return stats

    # -- tests -----------------------------------------------------------

    def test_lower_revision_arriving_late_cannot_overwrite(self):
        # rev 10 is committed but its response is withheld (post-commit gate).
        late = self.publisher("rev10-late", 10, sync_point="post-commit")
        # While it hangs, a newer revision is published by another process.
        status, body = self.cluster.publish(2, ACME, snapshot(ACME, 11,
                                                              ACCOUNTS_ENABLED, BINDINGS_ENABLED))
        self.assertEqual(status, 201, body)
        self.assert_revision(11)

        # Now release the late lower revision. Its commit already happened
        # before rev 11, so it reports success -- but the directory must stay
        # at 11 and its snapshot is now stale for future retries.
        result = late.release()
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.result["http_status"], 201, result.result)
        self.assertEqual(result.result["response"]["revision"], 10)
        self.assert_revision(11)

        # Retrying the original rev-10 snapshot is rejected as stale.
        retry = self.cluster.publish(3, ACME, late.payload)
        self.assertEqual(retry[0], 409, retry[1])
        self.assertEqual(retry[1]["error"]["code"], "stale_revision")
        self.assert_revision(11)

    def test_late_low_revision_rejected_at_open(self):
        """A rev-10 request that only reaches the database after rev 11 commits.

        The publisher is paused at ``pre-open`` (validation done, no transaction
        yet). A higher revision is published, then the paused request proceeds.
        It must observe current=11 under its own write lock and be rejected as
        stale, leaving revision 11 in place.
        """
        early_low = self.publisher("low-late", 10, sync_point="pre-open")
        status, body = self.cluster.publish(
            2, ACME, snapshot(ACME, 11, ACCOUNTS_ENABLED, BINDINGS_ENABLED)
        )
        self.assertEqual(status, 201, body)
        result = early_low.release()
        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertEqual(result.result["http_status"], 409, result.result)
        self.assertEqual(result.result["response"]["error"]["code"], "stale_revision")
        self.assertEqual(
            result.result["response"]["error"]["details"]["current_revision"], 11
        )
        self.assert_revision(11)

    def test_same_revision_different_content_serialised_by_gate(self):
        first = self.publisher("same-rev-a", 20, sync_point="pre-commit")
        # B blocks on the write lock; it is a separate process with its own
        # content for the same revision.
        payload_b = snapshot(
            ACME, 20,
            [account("acme-1", "b@acme.example", "Different content", True)],
            [],
        )
        thread_result: dict[str, object] = {}

        def publish_b():
            thread_result["value"] = self.cluster.publish(3, ACME, payload_b)

        thread = threading.Thread(target=publish_b)
        thread.start()
        time.sleep(0.3)  # let B reach BEGIN IMMEDIATE and block on the lock
        first.release()
        thread.join(timeout=60)
        self.assertIn("value", thread_result)
        status_b, body_b = thread_result["value"]  # type: ignore[misc]
        self.assertEqual(status_b, 409, body_b)
        self.assertEqual(body_b["error"]["code"], "revision_conflict")
        self.assert_revision(20)
        # A's content is the one that won
        status, body = self.cluster.resolve(0, ACME, "corp-idp-adapter", ACME_IDP, "sub-1")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["account_id"], "acme-1")

    def test_same_revision_identical_content_concurrent_is_single_winner(self):
        from tests.support import finish_publisher, start_publisher

        payload = snapshot(ACME, 21, ACCOUNTS_ENABLED, BINDINGS_ENABLED)
        procs = []
        for index in range(4):
            path = self.env.write_snapshot(f"same-rev-{index}", payload)
            procs.append((index, *start_publisher(
                self.env, ACME, path, self.cluster[index].base_url, name=f"same-{index}"
            )))

        statuses = []
        for index, proc, result_path, stderr_path in procs:
            result = finish_publisher(proc, result_path, stderr_path)
            statuses.append(result.result["http_status"])
        # Exactly one 201; the others either observed the identical content
        # already published (200 idempotent) or lost the insert race (409).
        self.assertEqual(statuses.count(201), 1, statuses)
        self.assertTrue(all(s in (201, 200, 409) for s in statuses), statuses)
        self.assert_revision(21)

    def test_same_revision_different_content_concurrent_has_one_winner(self):
        from tests.support import start_publisher, finish_publisher

        procs = []
        for index in range(4):
            payload = snapshot(
                ACME, 22,
                [account(f"acme-{index}", f"u{index}@acme.example", f"U{index}", True)],
                [identity(ACME_IDP, "shared-sub", f"acme-{index}")],
            )
            path = self.env.write_snapshot(f"race-{index}", payload)
            procs.append((index, *start_publisher(
                self.env, ACME, path, self.cluster[index].base_url, name=f"race-{index}"
            )))

        statuses = []
        bodies = []
        for index, proc, result_path, stderr_path in procs:
            result = finish_publisher(proc, result_path, stderr_path)
            statuses.append(result.result["http_status"])
            bodies.append(result.result["response"])
        self.assertEqual(statuses.count(201), 1, (statuses, bodies))
        for status, body in zip(statuses, bodies):
            if status != 201:
                self.assertEqual(status, 409, body)
                self.assertEqual(body["error"]["code"], "revision_conflict", body)
        winner = bodies[statuses.index(201)]["content_hash"]
        self.assertEqual(self.assert_revision(22)["content_hash"], winner)

    def test_no_half_applied_directory_switch(self):
        """All-allowed -> all-disabled must be observed atomically by batches."""
        self.cluster.publish(0, ACME, snapshot(ACME, 30, ACCOUNTS_ENABLED, BINDINGS_ENABLED))
        self.assert_revision(30)

        # Batch under revision 30: everyone allowed.
        status, body = self.cluster.batch(1, ACME, "corp-idp-adapter", BATCH_IDENTITIES)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revision"], 30)
        self.assertEqual(body["resolved_count"], 5)
        self.assertTrue(body["all_resolved"])

        # Hold revision 31 (all disabled, no bindings) at pre-commit. While it
        # is held, readers must still see 30 in full.
        gate = self.publisher("deny-all", 31, enabled=False, identities=[],
                              sync_point="pre-commit")
        status, body = self.cluster.batch(2, ACME, "corp-idp-adapter", BATCH_IDENTITIES)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revision"], 30)
        self.assertEqual(body["resolved_count"], 5)

        # While the gate is held, launch a stream of batches on the other
        # servers; every response must be internally consistent.
        stop = threading.Event()
        failures: list[str] = []
        seen_revisions: set[int] = set()

        def batch_loop(server_index: int) -> None:
            while not stop.is_set():
                try:
                    status, body = self.cluster.batch(
                        server_index, ACME, "corp-idp-adapter", BATCH_IDENTITIES
                    )
                except Exception as exc:  # pragma: no cover - connection noise
                    failures.append(f"server {server_index}: {type(exc).__name__}: {exc}")
                    return
                if status != 200:
                    failures.append(f"status {status}: {body}")
                    return
                revisions = {r["revision"] for r in body["results"]}
                if revisions != {body["revision"]}:
                    failures.append(f"mixed revisions inside one response: {body}")
                    return
                resolved = body["resolved_count"]
                if body["revision"] == 30 and resolved != 5:
                    failures.append(f"revision 30 half-applied: {body}")
                    return
                if body["revision"] == 31 and resolved != 0:
                    failures.append(f"revision 31 half-applied: {body}")
                    return
                seen_revisions.add(body["revision"])
                time.sleep(0.002)

        threads = [threading.Thread(target=batch_loop, args=(i,)) for i in (1, 2, 3)]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        gate.release()
        time.sleep(0.5)
        stop.set()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(failures, [])
        self.assertTrue({30, 31}.issubset(seen_revisions), seen_revisions)
        self.assert_revision(31)

        # After the switch, everyone is refused, and all from revision 31.
        status, body = self.cluster.batch(0, ACME, "corp-idp-adapter", BATCH_IDENTITIES)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revision"], 31)
        self.assertEqual(body["resolved_count"], 0)
        for item in body["results"]:
            self.assertFalse(item["resolved"])
            self.assertEqual(item["revision"], 31)

    def test_publish_completion_implies_visibility(self):
        # Publish through server 0, then immediately query server 3 (a
        # different process): it must see the new revision or newer.
        for revision in (40, 41, 42):
            status, body = self.cluster.publish(
                0, ACME, snapshot(ACME, revision, ACCOUNTS_ENABLED, BINDINGS_ENABLED)
            )
            self.assertEqual(status, 201, body)
            stats = self.cluster.stats(3, ACME)
            self.assertGreaterEqual(stats["revision"], revision, stats)

    def test_many_processes_higher_revision_wins(self):
        """Four revisions published from four independent processes.

        Without gates the commit order is nondeterministic, but the invariant
        is not: the final revision must equal the highest successful one, and
        no success may claim a revision that was never the highest at commit
        time (that would mean an overwrite).
        """
        from tests.support import start_publisher, finish_publisher

        procs = []
        for index, revision in enumerate((50, 51, 52, 53)):
            path = self.env.write_snapshot(
                f"order-{revision}",
                snapshot(ACME, revision, ACCOUNTS_ENABLED, BINDINGS_ENABLED),
            )
            procs.append((revision, *start_publisher(
                self.env, ACME, path, self.cluster[index].base_url, name=f"order-{revision}"
            )))
        results = []
        for revision, proc, result_path, stderr_path in procs:
            result = finish_publisher(proc, result_path, stderr_path)
            results.append((revision, result.result["http_status"], result.result["response"]))
        successes = [rev for rev, status, _ in results if status == 201]
        self.assertTrue(successes, results)
        final = self.assert_revision(max(successes))
        self.assertEqual(final["revision"], 53, results)


if __name__ == "__main__":
    unittest.main()
