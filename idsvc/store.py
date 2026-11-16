"""SQLite persistence for tenant directories.

All cross-process correctness lives in SQLite transactions (BEGIN IMMEDIATE +
UNIQUE constraints + WAL snapshots); there is no in-process lock anywhere in
the write or read paths.

Tables
------
meta(key, value)                 schema version marker
tenants(tenant, current_revision)  the publication pointer per tenant
revisions(tenant, revision, content_hash, accounts_json, identities_json,
          published_at, publication_count)
    Every accepted revision is retained forever, which is what makes the
    idempotency decision ("same revision, equivalent content") persistent
    across restarts.
bindings(tenant, issuer, subject, revision, account_id, content_hash)
    Only the bindings of the currently published revision. The primary key
    (tenant, issuer, subject) makes cross-account duplication of an external
    identity impossible at the database level, and the whole set is replaced
    inside the same transaction that advances tenants.current_revision.

The published snapshot is therefore exactly "the row in revisions that
tenants.current_revision points at" plus "the bindings rows carrying that
revision". A crash cannot leave a partially applied snapshot because the
pointer flip and the binding replacement commit atomically.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import NotFound, RevisionConflict, RevisionTaken, StaleRevision, StoreBusy
from .snapshot import Account, Snapshot

SCHEMA_VERSION = "1"
DEFAULT_BUSY_TIMEOUT_MS = 15_000

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    tenant            TEXT PRIMARY KEY,
    current_revision  INTEGER NOT NULL CHECK (current_revision > 0)
);
CREATE TABLE IF NOT EXISTS revisions (
    tenant             TEXT    NOT NULL,
    revision           INTEGER NOT NULL CHECK (revision > 0),
    content_hash       TEXT    NOT NULL,
    accounts_json      TEXT    NOT NULL,
    identities_json    TEXT    NOT NULL,
    published_at       TEXT    NOT NULL,
    publication_count  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant, revision),
    FOREIGN KEY (tenant) REFERENCES tenants(tenant) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS bindings (
    tenant       TEXT NOT NULL,
    issuer       TEXT NOT NULL,
    subject      TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    account_id   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (tenant, issuer, subject),
    -- The materialised binding rows must always belong to a stored revision,
    -- so a binding can never outlive or precede the revision it represents.
    FOREIGN KEY (tenant, revision) REFERENCES revisions(tenant, revision)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS bindings_by_revision ON bindings(tenant, revision);
"""


def utc_now_iso() -> str:
    # timezone-aware UTC; no locale-dependent formatting.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


@dataclass(frozen=True)
class PublishOutcome:
    tenant: str
    revision: int
    outcome: str  # "published" | "unchanged"
    content_hash: str
    publication_count: int

    @property
    def idempotent(self) -> bool:
        return self.outcome == "unchanged"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "revision": self.revision,
            "outcome": self.outcome,
            "idempotent": self.idempotent,
            "content_hash": self.content_hash,
            "publication_count": self.publication_count,
        }


@dataclass(frozen=True)
class CurrentDirectory:
    tenant: str
    revision: int
    content_hash: str
    accounts: dict[str, Account]
    bindings: dict[tuple[str, str], str]  # (issuer, subject) -> account_id


class Store:
    def __init__(self, path: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        self.path = os.path.abspath(path)
        self.busy_timeout_ms = busy_timeout_ms

    # -- connections -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # explicit BEGIN/COMMIT only
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = %d" % self.busy_timeout_ms)
        conn.execute("PRAGMA foreign_keys = ON")
        # synchronous is per-connection, not persisted: set it on every
        # connection so a COMMIT response implies a durable commit.
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def read_connection(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.execute("PRAGMA query_only = ON")
        return conn

    def initialize(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def schema_version(self) -> str | None:
        if not os.path.exists(self.path):
            return None
        try:
            conn = self.read_connection()
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                return None if row is None else str(row["value"])
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise

    # -- publication -----------------------------------------------------

    def publish(
        self,
        snapshot: Snapshot,
        *,
        pre_open_hook=None,
        pre_commit_hook=None,
        post_commit_hook=None,
    ) -> PublishOutcome:
        """Publish a fully validated snapshot atomically.

        Hooks are test synchronisation points: ``pre_open_hook`` runs before
        the write transaction exists (the request has had no effect yet),
        ``pre_commit_hook`` runs after the write lock is taken but before
        COMMIT, ``post_commit_hook`` runs after COMMIT and before the caller
        can build an HTTP response.
        """
        if pre_open_hook is not None:
            pre_open_hook("before-open")
        canonical = snapshot.canonical()
        accounts_json = json.dumps(canonical["accounts"], sort_keys=True, separators=(",", ":"))
        identities_json = json.dumps(canonical["identities"], sort_keys=True, separators=(",", ":"))
        content_hash = snapshot.content_hash()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT current_revision FROM tenants WHERE tenant = ?", (snapshot.tenant,)
                ).fetchone()
                current = None if row is None else int(row["current_revision"])

                if current is not None:
                    if snapshot.revision < current:
                        raise StaleRevision(
                            f"revision {snapshot.revision} is older than the published revision "
                            f"{current} for tenant {snapshot.tenant!r}",
                            details={"tenant": snapshot.tenant, "current_revision": current,
                                     "submitted_revision": snapshot.revision},
                        )
                    if snapshot.revision == current:
                        existing = conn.execute(
                            "SELECT content_hash FROM revisions WHERE tenant=? AND revision=?",
                            (snapshot.tenant, current),
                        ).fetchone()
                        if existing is None:  # pragma: no cover - schema invariant
                            raise RevisionConflict(
                                f"tenant pointer at revision {current} but revision row is missing"
                            )
                        if existing["content_hash"] != content_hash:
                            raise RevisionConflict(
                                f"revision {current} is already published for tenant "
                                f"{snapshot.tenant!r} with different content",
                                details={"tenant": snapshot.tenant, "revision": current,
                                         "published_content_hash": existing["content_hash"],
                                         "submitted_content_hash": content_hash},
                            )
                        conn.execute(
                            "UPDATE revisions SET publication_count = publication_count + 1 "
                            "WHERE tenant=? AND revision=?",
                            (snapshot.tenant, current),
                        )
                        count_row = conn.execute(
                            "SELECT publication_count FROM revisions WHERE tenant=? AND revision=?",
                            (snapshot.tenant, current),
                        ).fetchone()
                        outcome = PublishOutcome(
                            tenant=snapshot.tenant,
                            revision=current,
                            outcome="unchanged",
                            content_hash=content_hash,
                            publication_count=int(count_row["publication_count"]),
                        )
                        if pre_commit_hook is not None:
                            pre_commit_hook("idempotent")
                        conn.execute("COMMIT")
                        if post_commit_hook is not None:
                            post_commit_hook("idempotent")
                        return outcome

                # New revision: insert the tenant row first (FK ordering), then
                # the revision, then atomically replace the whole binding set.
                # Uniqueness of (tenant, issuer, subject) is enforced by the
                # bindings primary key against the final set only, so a
                # snapshot that swaps two targets is legal. The pointer update
                # is part of the same transaction, so no reader can see the
                # new pointer with old bindings or vice versa.
                conn.execute(
                    "INSERT INTO tenants(tenant, current_revision) VALUES(?,?) "
                    "ON CONFLICT(tenant) DO UPDATE SET current_revision = excluded.current_revision",
                    (snapshot.tenant, snapshot.revision),
                )
                conn.execute(
                    "INSERT INTO revisions(tenant, revision, content_hash, accounts_json, "
                    "identities_json, published_at, publication_count) VALUES(?,?,?,?,?,?,1)",
                    (snapshot.tenant, snapshot.revision, content_hash, accounts_json,
                     identities_json, utc_now_iso()),
                )
                conn.execute("DELETE FROM bindings WHERE tenant=?", (snapshot.tenant,))
                conn.executemany(
                    "INSERT INTO bindings(tenant, issuer, subject, revision, account_id, content_hash) "
                    "VALUES(?,?,?,?,?,?)",
                    [
                        (snapshot.tenant, i.issuer, i.subject, snapshot.revision, i.account_id, content_hash)
                        for i in snapshot.identities
                    ],
                )
                if pre_commit_hook is not None:
                    pre_commit_hook("new")
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                # Only reachable if caller-side validation was bypassed; map to
                # a conflict rather than leaking a driver exception.
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise RevisionTaken(f"integrity constraint rejected snapshot: {exc}") from None
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

            if post_commit_hook is not None:
                post_commit_hook("new")
            return PublishOutcome(
                tenant=snapshot.tenant,
                revision=snapshot.revision,
                outcome="published",
                content_hash=content_hash,
                publication_count=1,
            )
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise StoreBusy(f"database busy while publishing: {exc}") from None
            raise
        finally:
            conn.close()

    # -- reads -----------------------------------------------------------

    def current_revision(self, tenant: str) -> int | None:
        conn = self.read_connection()
        try:
            row = conn.execute(
                "SELECT current_revision FROM tenants WHERE tenant = ?", (tenant,)
            ).fetchone()
            return None if row is None else int(row["current_revision"])
        finally:
            conn.close()

    def _load_in_transaction(self, conn: sqlite3.Connection, tenant: str) -> CurrentDirectory:
        row = conn.execute(
            "SELECT t.current_revision AS rev, r.content_hash AS hash, r.accounts_json AS acc "
            "FROM tenants t JOIN revisions r "
            "  ON r.tenant = t.tenant AND r.revision = t.current_revision "
            "WHERE t.tenant = ?",
            (tenant,),
        ).fetchone()
        if row is None:
            raise NotFound(f"tenant {tenant!r} has no published directory")
        revision = int(row["rev"])
        accounts = {
            a["account_id"]: Account(
                account_id=a["account_id"],
                email=a["email"],
                display_name=a["display_name"],
                enabled=bool(a["enabled"]),
            )
            for a in json.loads(row["acc"])
        }
        bindings: dict[tuple[str, str], str] = {}
        for b in conn.execute(
            "SELECT issuer, subject, account_id FROM bindings WHERE tenant=? AND revision=?",
            (tenant, revision),
        ):
            bindings[(b["issuer"], b["subject"])] = b["account_id"]
        return CurrentDirectory(
            tenant=tenant,
            revision=revision,
            content_hash=str(row["hash"]),
            accounts=accounts,
            bindings=bindings,
        )

    def read_current(self, tenant: str) -> CurrentDirectory:
        """Read one consistent published version.

        The JOIN between the pointer and the revision row plus the binding
        rows are all served from a single read transaction (implicitly opened
        by the first SELECT in WAL mode and closed at COMMIT/ROLLBACK), so a
        concurrent publish can never produce a mixed view.
        """
        conn = self.read_connection()
        try:
            conn.execute("BEGIN")
            try:
                result = self._load_in_transaction(conn, tenant)
            finally:
                conn.execute("ROLLBACK")
            return result
        finally:
            conn.close()

    def resolve_one(self, tenant: str, issuer: str, subject: str) -> tuple[CurrentDirectory, str | None]:
        directory = self.read_current(tenant)
        return directory, directory.bindings.get((issuer, subject))

    def resolve_many(
        self, tenant: str, identities: Iterable[tuple[str, str]]
    ) -> tuple[CurrentDirectory, list[str | None]]:
        """Resolve all identities against exactly one published revision."""
        wanted = list(identities)
        conn = self.read_connection()
        try:
            conn.execute("BEGIN")
            try:
                directory = self._load_in_transaction(conn, tenant)
            finally:
                conn.execute("ROLLBACK")
        finally:
            conn.close()
        return directory, [directory.bindings.get(key) for key in wanted]

    # -- diagnostics -----------------------------------------------------

    def stats(self, tenant: str) -> dict[str, Any]:
        conn = self.read_connection()
        try:
            row = conn.execute(
                "SELECT t.current_revision AS rev, r.content_hash AS hash, "
                "  r.published_at AS published_at, r.publication_count AS pc, "
                "  r.accounts_json AS acc, r.identities_json AS ids "
                "FROM tenants t JOIN revisions r "
                "  ON r.tenant=t.tenant AND r.revision=t.current_revision "
                "WHERE t.tenant=?",
                (tenant,),
            ).fetchone()
            if row is None:
                raise NotFound(f"tenant {tenant!r} has no published directory")
            return {
                "tenant": tenant,
                "revision": int(row["rev"]),
                "content_hash": row["hash"],
                "published_at": row["published_at"],
                "publication_count": int(row["pc"]),
                "account_count": len(json.loads(row["acc"])),
                "identity_count": len(json.loads(row["ids"])),
            }
        finally:
            conn.close()

    def list_tenants(self) -> list[dict[str, Any]]:
        conn = self.read_connection()
        try:
            rows = conn.execute(
                "SELECT tenant, current_revision FROM tenants ORDER BY tenant"
            ).fetchall()
            return [{"tenant": r["tenant"], "revision": int(r["current_revision"])} for r in rows]
        finally:
            conn.close()

    def history(self, tenant: str) -> list[dict[str, Any]]:
        conn = self.read_connection()
        try:
            rows = conn.execute(
                "SELECT revision, content_hash, published_at, publication_count "
                "FROM revisions WHERE tenant=? ORDER BY revision",
                (tenant,),
            ).fetchall()
            return [
                {
                    "revision": int(r["revision"]),
                    "content_hash": r["content_hash"],
                    "published_at": r["published_at"],
                    "publication_count": int(r["publication_count"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def account(self, tenant: str, account_id: str) -> dict[str, Any]:
        directory = self.read_current(tenant)
        account = directory.accounts.get(account_id)
        if account is None:
            raise NotFound(f"account {account_id!r} is not in the published directory")
        return {
            "tenant": tenant,
            "revision": directory.revision,
            "account_id": account.account_id,
            "email": account.email,
            "display_name": account.display_name,
            "enabled": account.enabled,
            "identities": [
                {"issuer": issuer, "subject": subject}
                for (issuer, subject), target in sorted(directory.bindings.items())
                if target == account_id
            ],
        }

    def bindings(self, tenant: str) -> dict[str, Any]:
        directory = self.read_current(tenant)
        return {
            "tenant": tenant,
            "revision": directory.revision,
            "bindings": [
                {"issuer": issuer, "subject": subject, "account_id": account_id}
                for (issuer, subject), account_id in sorted(directory.bindings.items())
            ],
        }
