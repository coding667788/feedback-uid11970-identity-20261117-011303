# Design and correctness argument

## 1. Data model

```
tenants(tenant PK, current_revision)          -- one publication pointer per tenant
revisions(tenant, revision, content_hash,      -- every accepted revision, forever
          accounts_json, identities_json,
          published_at, publication_count)
bindings(tenant, issuer, subject, revision, account_id, content_hash)
        PK (tenant, issuer, subject)           -- current snapshot only
```

The *published directory of a tenant* is defined as exactly:

* the `revisions` row that `tenants.current_revision` points at (accounts), plus
* the `bindings` rows whose `revision` equals that pointer.

There is no separate "is current" flag to drift out of sync, and no per-account
`deleted` tombstone: absence from the current revision is the absence of the
account. That is what makes "whole-snapshot replacement" fall out of the schema
rather than out of application discipline.

`revisions` retains history because idempotency ("same revision, equivalent
content") and conflict detection are storage decisions, not in-memory ones.
A process restart cannot forget whether revision 12 was already accepted or
what its content hash was.

## 2. Publication transaction

One `BEGIN IMMEDIATE` ... `COMMIT` per publication:

1. Read `tenants.current_revision` **inside the write lock**. `BEGIN IMMEDIATE`
   takes SQLite's RESERVED lock up front, so no other writer can commit between
   this read and our commit.
2. If `submitted < current`: raise `stale_revision`, roll back.
3. If `submitted == current`:
   * compare the stored `content_hash` with the submitted canonical hash;
   * equal -> bump `publication_count`, report `unchanged` (HTTP 200);
   * different -> `revision_conflict` (HTTP 409), roll back.
4. If `submitted > current`:
   * upsert the tenant pointer (the INSERT that satisfies the revision's FK);
   * insert the `revisions` row;
   * `DELETE FROM bindings WHERE tenant = ?`;
   * insert the new binding set;
   * commit.

Consequences:

* **No partial snapshots.** The pointer is written in the same transaction as
  the revision row and the binding set. A crash before COMMIT leaves nothing;
  a crash after COMMIT leaves everything. There is no state in which the
  pointer names revision N while the bindings are a mix of N-1 and N.
* **No intermediate uniqueness violation on swaps.** Uniqueness of
  `(tenant, issuer, subject)` is checked by SQLite at statement end within the
  transaction. Since the old binding set is deleted and the new one inserted
  before COMMIT, two bindings that exchange target accounts are valid: no
  intermediate state ever has both rows mapped to the same account, because
  bindings are not unique on `account_id` at all. ("One external identity maps
  to one account" is the only uniqueness rule, and it is enforced by the PK.)
* **Lower revisions arrive late but lose.** A publisher whose `BEGIN IMMEDIATE`
  runs after a higher revision committed reads the higher `current_revision`
  and is rejected as stale. A publisher that committed earlier and is merely
  slow to respond has already published its lower revision; a *later* request
  for an older revision is stale. The final state is always the highest
  successfully published revision because no commit can ever lower the pointer.
* **Same-revision races.** Two different bodies at revision N: the first
  commit stores hash H1; the second, under the lock, compares H2 != H1 and gets
  `conflict`. Two identical bodies: the second sees H1 == H1 and returns
  idempotent success. Both outcomes are durable.

## 3. Reads

`read_current` (and therefore every resolve/batch) opens a read transaction,
loads the pointer + revision row in one JOIN and the bindings in the same
transaction, then ends the transaction. In WAL mode a read transaction sees one
consistent committed snapshot for its whole lifetime and never blocks a writer.
A batch therefore cannot mix revision N and N+1: it decodes one
`CurrentDirectory` struct and answers every identity from that struct, echoing
its single `revision` in every result.

Visibility after publication: COMMIT completes before the HTTP response is
written (`post_commit_hook` runs before the handler serialises the body), so a
publish response implies durability. Any read begun afterwards starts a new
WAL snapshot at or after that commit. This is why
`test_publish_completion_implies_visibility` can publish through one process and
immediately read through another.

## 4. Why no in-process lock

Nothing in `idsvc` uses `threading.Lock` for correctness. `ThreadingHTTPServer`
serves requests on threads, and all shared state is inside SQLite. The relevant
guarantees come from `BEGIN IMMEDIATE`, the primary keys, `foreign_keys=ON`, WAL
isolation and the 15 s `busy_timeout`. Correctness therefore survives having
four processes rather than four threads, and would survive four machines sharing
the file over a network filesystem *only as well as SQLite's own file locking
does* — this design targets one local file, as specified.

## 5. Schema strictness

Validation is deliberately pedantic so that no request can mean two things:

* `json.loads` is called with `object_pairs_hook` rejecting duplicate keys and
  `parse_constant` rejecting `NaN`/`Infinity`; trailing data is a parse error.
* Every object is closed (exact key sets). Unknown fields are errors, not
  warnings.
* `revision` must be a JSON integer; `bool` is checked before `int` because
  Python's `bool` is an `int` subclass. `1.0`, `"1"`, `true` all fail.
* `enabled` must be a JSON boolean; `1`/`0`/`"true"` fail.
* Strings are validated for type, non-emptiness where required, length, and
  absence of unpaired surrogates (which cannot be stored as UTF-8).
* `tenant` in the body must equal the URL tenant.
* Account and identity collections are validated as sets: duplicate
  `account_id`, duplicate `(issuer, subject)`, and bindings referencing absent
  accounts all reject the whole snapshot before any transaction starts.

No transformation happens anywhere: no `.strip()`, `.lower()`, `.casefold()`,
`unicodedata.normalize`, or `urllib.parse` call touches `issuer`/`subject`.
They are stored and compared as the exact decoded JSON strings.

Canonical equivalence is computed from a sorted representation: accounts sorted
by `account_id`, identities sorted by `(issuer, subject, account_id)`, object
keys sorted, compact separators, ASCII-escaped, SHA-256. So the same semantic
snapshot reordered is byte-equal after canonicalisation, while any change in a
string, a type, or `enabled` changes the hash.

## 6. Failure injection and process death

Two sync points are exposed only when a caller sends headers:

| Point | Position in the transaction | What a kill here leaves |
|---|---|---|
| `pre-open` | after validation, no transaction yet | nothing at all; the request is a no-op |
| `pre-commit` | after all writes, inside the open write transaction | uncommitted pages; SQLite rolls back on connection close |
| `post-commit` | after COMMIT, before the response is written | a fully committed revision the client never heard about |

The mechanism is a *gate file*: the process writes `pid`, point and timestamp,
then polls until the file disappears. The harness deletes it to release the
process or calls `Popen.kill()` to destroy it. Nothing is simulated with a
raised exception; the tests observe real exit, then restart services and read
the database through HTTP.

The tests kill:

* a **direct-DB publisher** at `pre-commit` (it owns the transaction);
* a **direct-DB publisher** at `post-commit` (commit is durable, response lost);
* a **service process** at `pre-commit` (the HTTP client sees a dropped
  connection; the open transaction must roll back);
* a **service process** at `post-commit` (the revision survives without a
  response);
* a blocked waiter scenario: a second publisher queues behind a gated writer,
  the writer is killed, and the waiter must acquire the lock and succeed.

After every kill the test restarts all four services, re-reads the directory,
retries the original snapshot, and asserts: whole-snapshot atomicity (revision
counts and bindings all match one revision), idempotent success for an
already-committed retry, `conflict` for a changed body at the same revision,
`stale` for an older one, and that the revision never decreases.

One separate ordering case uses `pre-open`: a rev-10 request is validated and
parked before it opens a transaction; rev 11 is published; the parked request
then proceeds and must be rejected `stale` (not applied). This is the
deterministic form of "a late low revision must not overwrite".

## 7. Credentials

`init` writes `runtime/adapters.json` with 32-byte URL-safe random tokens. The
database has no credential column. The API never echoes the token (the response
carries only `adapter_id`). CLI commands read the token from the file and never
print it. `tests/run_all.py` scans every log, result file and database dump
produced by the run for the exact token strings and fails on any hit.

## 8. Trade-offs and limits

* Whole-tenant snapshots are required by the brief; large tenants mean large
  PUT bodies (capped at 32 MiB) and full account JSON reparsed per read.
  Accounts are stored as one JSON document per revision, which is simple and
  consistent but re-parses on each directory load. A production design would
  split accounts into rows and cache; the fail-stop consistency argument would
  be unchanged.
* `publication_count` is an observability aid, not a business counter.
* `POST /v1/tenants/{t}/resolve-batch` returns per-identity allow/deny; it is
  atomic in *version*, not in allow/deny (a batch may contain some refused
  identities by design, since the caller is asking "who among these may sign
  in").
* Anything not listed as implemented (token refresh, password login, external
  IdP calls) is absent rather than stubbed.
