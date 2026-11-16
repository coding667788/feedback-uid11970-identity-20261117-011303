"""Strict snapshot schema validation and canonical equivalence.

Schema (documented in docs/SCHEMA.md; every object is closed: unknown keys are
rejected):

  root            = {tenant: TenantStr, revision: PositiveInt,
                     accounts: [Account], identities: [Identity]}
  Account         = {account_id: Str, email: Str, display_name: Str,
                     enabled: Bool}
  Identity        = {issuer: Str, subject: Str, account_id: Str}

The identity list may alternatively be named ``bindings`` or
``external_identities`` (exactly one of the three keys, never two).
``tenant`` may be omitted from the body when it is supplied in the URL; if
present it must match the URL tenant.

Strings are compared and stored byte-for-byte after JSON decoding: no case
folding, whitespace trimming, Unicode normalisation or URL normalisation.
JSON types are enforced exactly: ``true`` is not a revision, ``"1"`` is not a
revision, ``1`` is not an enabled flag.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError

ROOT_KEYS = {"tenant", "revision", "accounts", "identities", "bindings", "external_identities"}
IDENTITY_LIST_KEYS = ("identities", "bindings", "external_identities")
ACCOUNT_KEYS = {"account_id", "email", "display_name", "enabled"}
IDENTITY_KEYS = {"issuer", "subject", "account_id"}

MAX_STRING_CHARS = 4096
MAX_ACCOUNTS = 100_000
MAX_IDENTITIES = 200_000
MAX_REPORTED_ERRORS = 25


@dataclass(frozen=True)
class Account:
    account_id: str
    email: str
    display_name: str
    enabled: bool


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    account_id: str


@dataclass(frozen=True)
class Snapshot:
    tenant: str
    revision: int
    accounts: tuple[Account, ...]  # sorted by account_id
    identities: tuple[Identity, ...]  # sorted by (issuer, subject, account_id)

    def canonical(self, *, tenant_from_body: bool = True) -> dict[str, Any]:
        """Order-independent canonical representation used for equivalence."""
        return {
            "tenant": self.tenant if tenant_from_body else None,
            "revision": self.revision,
            "accounts": [
                {
                    "account_id": a.account_id,
                    "email": a.email,
                    "display_name": a.display_name,
                    "enabled": a.enabled,
                }
                for a in self.accounts
            ],
            "identities": [
                {"issuer": i.issuer, "subject": i.subject, "account_id": i.account_id}
                for i in self.identities
            ],
        }

    def canonical_json(self) -> str:
        # ensure_ascii keeps the bytes independent of console/locale settings.
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValidationError(f"duplicate object key {key!r} is not allowed", path=key)
        seen.add(key)
    return dict(pairs)


def loads_strict(raw: bytes | str) -> Any:
    """Decode JSON rejecting duplicate keys, NaN/Infinity and trailing data."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"request body is not valid UTF-8: {exc}") from None
    else:
        text = raw

    def bad_constant(name: str) -> Any:
        raise ValidationError(f"non-standard JSON constant {name} is not allowed")

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=bad_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from None


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _check_string(value: Any, path: str, errors: list[str], *, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{path}: expected string, got {_type_name(value)}")
        return None
    if not allow_empty and value == "":
        errors.append(f"{path}: must be a non-empty string")
        return None
    if len(value) > MAX_STRING_CHARS:
        errors.append(f"{path}: string longer than {MAX_STRING_CHARS} characters")
        return None
    # Reject lone surrogates: they cannot be stored as UTF-8 and would make an
    # otherwise well-typed snapshot unpersistable.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        errors.append(f"{path}: string contains unpaired surrogate code points")
        return None
    return value


def _check_closed_object(value: Any, allowed: set[str], path: str, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{path}: expected object, got {_type_name(value)}")
        return False
    unknown = sorted(set(value) - allowed)
    if unknown:
        errors.append(f"{path}: unknown field(s) {unknown!r}; allowed fields are {sorted(allowed)!r}")
        return False
    return True


def validate_snapshot(body: Any, *, tenant: str) -> Snapshot:
    """Validate ``body`` for the tenant named in the URL and return a Snapshot.

    Raises ValidationError with every discovered problem (bounded) when the
    snapshot is not exactly conformant.
    """
    if not isinstance(tenant, str) or tenant == "":
        raise ValidationError("tenant must be a non-empty string", path="tenant")
    if not isinstance(body, dict):
        raise ValidationError(f"snapshot body must be a JSON object, got {_type_name(body)}")

    errors: list[str] = []
    if not _check_closed_object(body, ROOT_KEYS, "$", errors):
        raise ValidationError(errors[0], path="$")

    present_identity_keys = [k for k in IDENTITY_LIST_KEYS if k in body]
    if len(present_identity_keys) != 1:
        if not present_identity_keys:
            errors.append("$: missing required field 'identities'")
        else:
            errors.append(
                "$: identity list must use exactly one of "
                f"{list(IDENTITY_LIST_KEYS)}, found {present_identity_keys!r}"
            )
    if "tenant" in body:
        tenant_in_body = body["tenant"]
        if not isinstance(tenant_in_body, str):
            errors.append(f"$.tenant: expected string, got {_type_name(tenant_in_body)}")
        elif tenant_in_body != tenant:
            errors.append(
                f"$.tenant: body tenant {tenant_in_body!r} does not match URL tenant {tenant!r}"
            )

    revision = body.get("revision")
    if "revision" not in body:
        errors.append("$.revision: missing required field")
    elif isinstance(revision, bool) or not isinstance(revision, int):
        errors.append(
            f"$.revision: expected integer (JSON number without fraction), got {_type_name(revision)}"
        )
    elif revision <= 0:
        errors.append(f"$.revision: must be a positive integer, got {revision}")

    accounts_raw = body.get("accounts")
    if "accounts" not in body:
        errors.append("$.accounts: missing required field")
        accounts_raw = None
    elif not isinstance(accounts_raw, list):
        errors.append(f"$.accounts: expected array, got {_type_name(accounts_raw)}")
        accounts_raw = None
    elif len(accounts_raw) > MAX_ACCOUNTS:
        errors.append(f"$.accounts: more than {MAX_ACCOUNTS} entries")
        accounts_raw = None

    identities_raw = body.get(present_identity_keys[0]) if len(present_identity_keys) == 1 else None
    if identities_raw is not None and not isinstance(identities_raw, list):
        errors.append(
            f"$.{present_identity_keys[0]}: expected array, got {_type_name(identities_raw)}"
        )
        identities_raw = None
    elif isinstance(identities_raw, list) and len(identities_raw) > MAX_IDENTITIES:
        errors.append(f"$.{present_identity_keys[0]}: more than {MAX_IDENTITIES} entries")
        identities_raw = None
    identity_list_path = f"$.{present_identity_keys[0]}" if present_identity_keys else "$.identities"

    accounts: list[Account] = []
    account_ids: set[str] = set()
    if isinstance(accounts_raw, list):
        for index, item in enumerate(accounts_raw):
            path = f"$.accounts[{index}]"
            if not _check_closed_object(item, ACCOUNT_KEYS, path, errors):
                continue
            missing = sorted(ACCOUNT_KEYS - set(item))
            if missing:
                errors.append(f"{path}: missing required field(s) {missing!r}")
                continue
            account_id = _check_string(item["account_id"], f"{path}.account_id", errors)
            email = _check_string(item["email"], f"{path}.email", errors, allow_empty=True)
            display_name = _check_string(
                item["display_name"], f"{path}.display_name", errors, allow_empty=True
            )
            enabled = item["enabled"]
            if not isinstance(enabled, bool):
                errors.append(f"{path}.enabled: expected boolean, got {_type_name(enabled)}")
                enabled = None
            if account_id is None or email is None or display_name is None or enabled is None:
                continue
            if account_id in account_ids:
                errors.append(f"{path}.account_id: duplicate account_id {account_id!r} in snapshot")
                continue
            account_ids.add(account_id)
            accounts.append(Account(account_id, email, display_name, enabled))

    identities: list[Identity] = []
    identity_keys: set[tuple[str, str]] = set()
    if isinstance(identities_raw, list):
        for index, item in enumerate(identities_raw):
            path = f"{identity_list_path}[{index}]"
            if not _check_closed_object(item, IDENTITY_KEYS, path, errors):
                continue
            missing = sorted(IDENTITY_KEYS - set(item))
            if missing:
                errors.append(f"{path}: missing required field(s) {missing!r}")
                continue
            issuer = _check_string(item["issuer"], f"{path}.issuer", errors)
            subject = _check_string(item["subject"], f"{path}.subject", errors)
            account_id = _check_string(item["account_id"], f"{path}.account_id", errors)
            if issuer is None or subject is None or account_id is None:
                continue
            key = (issuer, subject)
            if key in identity_keys:
                errors.append(
                    f"{path}: duplicate external identity (issuer={issuer!r}, subject={subject!r}) "
                    "appears more than once in the snapshot"
                )
                continue
            identity_keys.add(key)
            if account_id not in account_ids:
                errors.append(
                    f"{path}.account_id: references account {account_id!r} that is not present "
                    "in this snapshot"
                )
                continue
            identities.append(Identity(issuer, subject, account_id))

    if errors:
        # Keep the response bounded but disclose all (capped) problems.
        shown = errors[:MAX_REPORTED_ERRORS]
        suffix = "" if len(errors) <= MAX_REPORTED_ERRORS else (
            f" (+{len(errors) - MAX_REPORTED_ERRORS} more)"
        )
        raise ValidationError(
            "snapshot rejected: " + "; ".join(shown) + suffix,
            path="$",
            index=None,
        )

    # Account order in the array is irrelevant to the directory but the
    # canonical form must be identical for equivalent snapshots.
    accounts.sort(key=lambda a: a.account_id)
    identities.sort(key=lambda i: (i.issuer, i.subject, i.account_id))
    assert isinstance(revision, int) and not isinstance(revision, bool)
    return Snapshot(tenant=tenant, revision=revision, accounts=tuple(accounts), identities=tuple(identities))


def load_snapshot_file(path: str, *, tenant: str) -> Snapshot:
    with open(path, "rb") as handle:
        raw = handle.read()
    return validate_snapshot(loads_strict(raw), tenant=tenant)
