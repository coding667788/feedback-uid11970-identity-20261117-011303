# Identity Directory Service — Specification

This document is the normative contract. `idsvc/snapshot.py` is its executable
counterpart; if the two ever disagree, treat this document as intent and the
code as the implementation to bring into line.

## 1. Tenancy, accounts and external identities

* A **tenant** is an opaque, case-sensitive string. Directories are strictly
  per-tenant: nothing is shared, inherited or looked up across tenants.
* An **account** has an immutable `account_id`. `email` and `display_name` are
  mutable attributes only. They are never used to match, merge or identify an
  account, and two accounts may share an email address — including inside one
  tenant — without any effect on resolution.
* An **external identity** is the triple `(tenant, issuer, subject)`. `issuer`
  and `subject` are opaque strings, matched byte-for-byte after JSON decoding:
  no case folding, no trimming, no NFC/NFKC normalisation, no URL
  canonicalisation. `"https://idp.example/oidc"` and
  `"https://IDP.example/oidc"` are different identities, as are `"sub"` and
  `"sub "`.
* One account may be bound to any number of external identities. One external
  identity may be bound to at most one account **within one published
  revision** of one tenant. The same `(issuer, subject)` may be bound in two
  different tenants; the tenant is part of the identity key, so those are
  different external identities.
* Validation of issuer strings happens only inside a tenant administer's
  snapshot. The service does not enforce a URL shape on `issuer`.

## 2. Publication model: whole-tenant snapshots

A publication replaces the entire directory of one tenant with a snapshot:

```
snapshot    = { tenant? , revision , accounts [ , identities ] }
account     = { account_id , email , display_name , enabled }
identity    = { issuer , subject , account_id }
```

* `revision` is a JSON integer `>= 1`. `true`, `1.0`, `"1"` are all rejected.
* `accounts` and the identity list are **complete**: anything absent from the
  new snapshot is no longer resolvable from the moment the snapshot is
  published. There is no partial update, no merge and no tombstone.
* `identities` may be spelled `identities`, `bindings`, or
  `external_identities`; exactly one of those keys must be present.
* `tenant` may be omitted (the URL supplies it) but if present must equal the
  tenant in the URL.
* All objects are **closed**: unknown fields are rejected. Root, account and
  identity objects each have an exact allowed key set (see §3).

### 2.1 Version ordering

| Submitted | Outcome | HTTP |
|---|---|---|
| `revision > current` | published, becomes current | 201 |
| `revision == current`, canonically equal | idempotent success; `publication_count` increments persist | 200 |
| `revision == current`, different content | conflict, nothing changes | 409 `revision_conflict` |
| `revision < current` | stale, nothing changes | 409 `stale_revision` |
| schema-invalid | whole snapshot rejected, nothing changes | 422 `invalid_snapshot` |

The decision is persisted: `revisions` keeps every revision ever accepted
together with its content hash, so a restart does not change idempotency,
staleness or conflict results. Losing a race on the same revision yields
`conflict` for the loser, because by the time it commits the winner's content
hash is stored.

### 2.2 Canonical equivalence

Two snapshots are equivalent iff:

* same `revision`;
* the account arrays, compared as sets, are equal on `(account_id, email,
  display_name, enabled)` with exact string equality and exact boolean types;
* the identity arrays, compared as sets, are equal on `(issuer, subject,
  account_id)` with exact string equality;
* object key order is ignored; the account array order and identity array
  order are ignored (accounts and identities are sorted before hashing).

Notably **not** ignored: string content (whitespace, case, Unicode form), JSON
types (`true` ≠ `1`, `1` ≠ `"1"`), and `enabled` differences. Canonical JSON is
`sort_keys`, compact separators, ASCII-escaped, SHA-256 hashed.

### 2.3 Whole-snapshot rejections

The following reject the entire snapshot and leave the previous directory
byte-identical:

* an identity referencing an `account_id` absent from `accounts`;
* duplicate `account_id` in `accounts`;
* duplicate `(issuer, subject)` in the identity list;
* any field of the wrong JSON type (`email` number, `enabled` string,
  `revision` boolean, non-string `issuer`, …);
* missing required fields, unknown fields, duplicate object keys in the raw
  JSON text, `NaN`/`Infinity`;
* empty `issuer`, `subject` or `account_id`; unpaired surrogates;
* a `tenant` in the body that differs from the URL tenant.

Validation runs completely before any write transaction is opened, so a
rejected snapshot cannot leave partial rows.

### 2.4 Binding swaps

Because a snapshot is validated as a set and applied as a whole inside one
transaction, two identities may swap their target accounts in a single
publication:

```
old: (idpA, s1) -> acct-1   (idpB, s2) -> acct-2
new: (idpA, s1) -> acct-2   (idpB, s2) -> acct-1
```

The previous binding set is deleted and the new one inserted in the same
transaction, so the intermediate state (which would transiently have
`(idpA, s1)` mapped to both accounts) never exists in the database and is never
visible to a reader.

## 3. Machine-readable schema

```
root      := object, exactly subset of
             {tenant?: string, revision: integer, accounts: array,
              identities|bindings|external_identities: array}
             (exactly one identity-list key)
account   := object, exactly {account_id: string, email: string,
                              display_name: string, enabled: boolean}
identity  := object, exactly {issuer: string, subject: string,
                              account_id: string}
string    := JSON string, ≤ 4096 chars (tenant/issuer/subject/account_id
             non-empty; email/display_name may be empty), valid Unicode
```

## 4. Resolution

`POST /v1/tenants/{tenant}/resolve` and
`POST /v1/tenants/{tenant}/resolve-batch` require
`Authorization: Bearer <adapter credential>`.

* The credential belongs to a pre-provisioned adapter record holding
  `(adapter_id, tenant, issuer)`. The credential authenticates the call; the
  adapter's tenant must equal the URL tenant, otherwise the call is rejected
  with `401 invalid_adapter_credentials`. There is no way to reach another
  tenant's directory by naming it.
* A single call returns `account_id`, `enabled` and the `revision` used.
* An unbound identity, or an account that is `enabled: false`, is refused
  (`403 identity_not_resolvable`). A refusal never falls back to another
  account with the same email; resolution is purely by
  `(tenant, issuer, subject)`.
* A batch call resolves every identity against **one** published revision. The
  response carries a single top-level `revision`; each result repeats it. A
  batch is not atomic with respect to allow/deny (each entry can be allowed or
  refused independently) but it is atomic with respect to *version*: a
  response can never mix revision 4 and revision 5 results.
* A publish that returns `201`/`200` is durable before the response is written;
  any query begun afterwards observes that revision or a later one.
  `sqlite3` `synchronous=FULL` in WAL mode plus the single write transaction
  give this guarantee.

## 5. Consistency and concurrency

* At least four independent service processes share one SQLite file.
* All correctness comes from SQLite: `BEGIN IMMEDIATE` serialises publishers,
  the `(tenant, revision)` primary key and `(tenant, issuer, subject)` primary
  key enforce uniqueness, and a read transaction (implicit in WAL) delivers a
  stable snapshot to every reader. No in-process lock participates.
* Publishing revision 7 while another process publishes revision 6: the
  transaction that commits last still checks `current_revision` from inside
  its write lock, so revision 6 either commits before 7 (and is then simply
  superseded) or sees `current=7` and is rejected as stale. A lower revision
  never replaces a higher one.
* Publishing the same revision with different content concurrently: exactly
  one commits; the rest see the stored content hash and get `conflict`.

## 6. Failure injection

`PUT …/directory` honours:

| Header | Meaning |
|---|---|
| `X-Sync-Point: pre-open` | after validation, before the write transaction is opened |
| `X-Sync-Point: pre-commit` | after `BEGIN IMMEDIATE` and all writes, before `COMMIT` |
| `X-Sync-Point: post-commit` | after `COMMIT`, before the HTTP response is written |
| `X-Sync-Gate: <ascii path>` | gate file created on arrival; process proceeds when the harness deletes it |
| `X-Sync-Gate-B64: <base64>` | same, for paths that are not Latin-1 safe (UTF-8 path, base64) |
| `X-Sync-Timeout: <s>` | max wait; default 120 |

Killing the process at `pre-commit` leaves an uncommitted transaction that
SQLite rolls back on connection loss. Killing it at `post-commit` leaves the
revision committed; the client sees no response. `pre-open` lets a test hold a
request before it touches the database at all, which is how the "late low
revision loses to an already-committed higher one" case is fixed
deterministically. All three are exercised by `tests/test_concurrency.py` and
`tests/test_process_kill.py` with real process termination (`Popen.kill`), not
by raising exceptions.

## 7. HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness + schema version |
| GET | `/v1/tenants` | tenants with their current revision |
| GET | `/v1/tenants/{t}` | current revision, hash, counts |
| GET | `/v1/tenants/{t}/accounts/{id}` | account attributes + its bindings |
| GET | `/v1/tenants/{t}/bindings` | current binding list |
| GET | `/v1/tenants/{t}/history` | accepted revisions, hashes, publication counts |
| PUT | `/v1/tenants/{t}/directory` | publish a full snapshot |
| POST | `/v1/tenants/{t}/resolve` | resolve one identity |
| POST | `/v1/tenants/{t}/resolve-batch` | resolve many identities in one revision |

Errors are `{"error": {"code", "message", "details"?, "path"?}}` with the status
codes from §2.1/§4.

## 8. Secrets

Adapter credentials live only in the adapter file (`init` creates it with
fresh random tokens). The directory database has no credential column, and the
API, CLI and tests never print a token. HTTP logs contain method, path and
status only.
