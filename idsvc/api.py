"""HTTP API over the shared SQLite store.

Endpoints (all JSON, UTF-8):

  GET  /healthz
  GET  /v1/tenants
  GET  /v1/tenants/{tenant}
  GET  /v1/tenants/{tenant}/accounts/{account_id}
  GET  /v1/tenants/{tenant}/bindings
  GET  /v1/tenants/{tenant}/history
  PUT  /v1/tenants/{tenant}/directory          (full-snapshot publication)
  POST /v1/tenants/{tenant}/resolve            (single)
  POST /v1/tenants/{tenant}/resolve-batch      (batch against one revision)

Resolve endpoints require ``Authorization: Bearer <adapter credential>``. The
credential selects a pre-provisioned adapter, and the adapter (not the request
body) determines the tenant. No tenant can be impersonated by naming it.

Optional test synchronisation points (``PUT`` only):
  X-Sync-Point: pre-open   -> hold after validation, before the write
                              transaction is opened (the request has had no
                              effect on the database yet)
  X-Sync-Point: pre-commit -> hold after the write lock is taken and the rows
                              are written, before COMMIT
  X-Sync-Point: post-commit -> hold after COMMIT, before the HTTP response
  X-Sync-Gate: <ascii file> -> gate file path; created when the point is
                               reached, polled until removed
  X-Sync-Gate-B64: <b64>    -> same, for paths that are not Latin-1 safe
                               (base64 of the UTF-8 path)
  X-Sync-Timeout: <seconds> -> how long to wait for the gate file to vanish
Without the header the hooks are no-ops.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .adapters import Adapter, AdapterRegistry
from .errors import (
    InvalidCredentials,
    InvalidRequest,
    NotFound,
    ServiceError,
    ValidationError,
)
from .policy import apply_policy
from .snapshot import MAX_STRING_CHARS, Snapshot, loads_strict, validate_snapshot
from .store import Store

TENANT_SEGMENT = "tenants"
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_BATCH_IDENTITIES = 10_000
SYNC_HEADER = "X-Sync-Point"
GATE_HEADER = "X-Sync-Gate"
GATE_B64_HEADER = "X-Sync-Gate-B64"
SYNC_TIMEOUT_HEADER = "X-Sync-Timeout"
SYNC_POINTS = ("pre-open", "pre-commit", "post-commit")


# ---------------------------------------------------------------------------
# Synchronisation points
# ---------------------------------------------------------------------------


def _wait_for_gate(gate: str, timeout_s: float, point: str) -> None:
    """Announce arrival by creating the gate file, then wait until it is gone.

    The harness removes the file to release the process. Killing the process
    while it waits leaves both the gate file and the SQLite lock state behind
    for inspection; nothing is simulated with exceptions.
    """
    absolute = os.path.abspath(gate)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"pid={os.getpid()} point={point} at={time.time():.6f}\n")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + timeout_s
    while os.path.exists(absolute):
        if time.monotonic() >= deadline:
            raise ServiceError(
                f"sync point {point!r} timed out after {timeout_s:g}s", status=504
            )
        time.sleep(0.005)


def _sync_hook(headers: Any, point: str) -> Callable[[str], None] | None:
    requested = (headers.get(SYNC_HEADER) or "").strip().lower()
    if requested == "":
        return None
    if requested not in SYNC_POINTS:
        raise InvalidRequest(f"{SYNC_HEADER} must be one of {list(SYNC_POINTS)}")
    if requested != point:
        return None
    gate = headers.get(GATE_HEADER)
    encoded = headers.get(GATE_B64_HEADER)
    if encoded:
        import base64

        try:
            gate = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
        except Exception:
            raise InvalidRequest(f"{GATE_B64_HEADER} must be base64 of a UTF-8 path") from None
    if not gate:
        raise InvalidRequest(f"{SYNC_HEADER} requires {GATE_HEADER} or {GATE_B64_HEADER}")
    try:
        timeout_s = float(headers.get(SYNC_TIMEOUT_HEADER) or 120.0)
    except (TypeError, ValueError):
        raise InvalidRequest(f"{SYNC_TIMEOUT_HEADER} must be a number of seconds") from None
    if not (0 < timeout_s <= 3600):
        raise InvalidRequest(f"{SYNC_TIMEOUT_HEADER} must be within (0, 3600]")

    def hook(_kind: str) -> None:
        _wait_for_gate(gate, timeout_s, point)

    return hook


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityRequest:
    issuer: str
    subject: str


class Application:
    def __init__(self, store: Store, adapters: AdapterRegistry) -> None:
        self.store = store
        self.adapters = adapters

    def dispatch(self, method: str, raw_path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        parsed = urllib.parse.urlsplit(raw_path)
        segments = [urllib.parse.unquote(s) for s in parsed.path.split("/") if s != ""]
        try:
            return self._dispatch(method, segments, headers, body)
        except ServiceError as exc:
            payload: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
            if exc.details is not None:
                payload["error"]["details"] = exc.details
            if isinstance(exc, ValidationError) and exc.path:
                payload["error"]["path"] = exc.path
            return exc.status, payload
        except Exception:  # pragma: no cover - never leak a traceback to the wire
            traceback.print_exc(file=sys.stderr)
            return 500, {"error": {"code": "internal_error", "message": "unexpected server error"}}

    def _dispatch(
        self, method: str, segments: list[str], headers: Any, body: bytes
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and segments == ["healthz"]:
            return 200, {"status": "ok", "schema_version": self.store.schema_version()}
        if method == "GET" and segments == ["v1", TENANT_SEGMENT]:
            return 200, {"tenants": self.store.list_tenants()}
        if len(segments) >= 3 and segments[0] == "v1" and segments[1] == TENANT_SEGMENT:
            tenant = segments[2]
            rest = segments[3:]
            if rest == [] and method == "GET":
                return 200, self.store.stats(tenant)
            if rest == ["directory"]:
                if method != "PUT":
                    raise InvalidRequest("directory publication requires PUT")
                return self.publish(tenant, headers, body)
            if rest == ["resolve"]:
                if method != "POST":
                    raise InvalidRequest("resolve requires POST")
                return self.resolve_one(tenant, headers, body)
            if rest == ["resolve-batch"]:
                if method != "POST":
                    raise InvalidRequest("resolve-batch requires POST")
                return self.resolve_batch(tenant, headers, body)
            if len(rest) == 2 and rest[0] == "accounts" and method == "GET":
                return 200, self.store.account(tenant, rest[1])
            if rest == ["bindings"] and method == "GET":
                return 200, self.store.bindings(tenant)
            if rest == ["history"] and method == "GET":
                return 200, {"tenant": tenant, "revisions": self.store.history(tenant)}
            if rest in (["directory"], ["resolve"], ["resolve-batch"], ["accounts"], ["bindings"], ["history"]):
                raise InvalidRequest(f"method {method} is not supported on this route")
        if method not in ("GET", "PUT", "POST", "DELETE"):
            raise InvalidRequest(f"method {method} is not supported")
        raise NotFound(f"no route for {method} /{'/'.join(segments)}")

    # -- publication -----------------------------------------------------

    def publish(self, tenant: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        if not body:
            raise ValidationError("request body must be a JSON snapshot object")
        snapshot: Snapshot = validate_snapshot(loads_strict(body), tenant=tenant)
        pre_open = _sync_hook(headers, "pre-open")
        pre = _sync_hook(headers, "pre-commit")
        post = _sync_hook(headers, "post-commit")
        outcome = self.store.publish(
            snapshot,
            pre_open_hook=pre_open,
            pre_commit_hook=pre,
            post_commit_hook=post,
        )
        status = 200 if outcome.outcome == "unchanged" else 201
        return status, outcome.as_dict()

    # -- resolution ------------------------------------------------------

    def _authenticate(self, headers: Any, tenant: str) -> Adapter:
        auth = headers.get("Authorization") or ""
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise InvalidCredentials(
                "resolve requires 'Authorization: Bearer <adapter credential>'"
            )
        adapter = self.adapters.authenticate(token.strip())
        if adapter.tenant != tenant:
            # Tenant isolation is enforced on the credential, not on the path:
            # an adapter provisioned for one tenant can never read another.
            raise InvalidCredentials(
                f"adapter {adapter.adapter_id!r} is not provisioned for this tenant"
            )
        return adapter

    def resolve_one(self, tenant: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        adapter = self._authenticate(headers, tenant)
        request = _parse_identity_request(body)
        directory, account_id = self.store.resolve_one(tenant, request.issuer, request.subject)
        resolution = apply_policy(directory, request.issuer, request.subject, account_id)
        return 200, {
            "tenant": tenant,
            "adapter_id": adapter.adapter_id,
            **resolution.as_dict(),
        }

    def resolve_batch(self, tenant: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        adapter = self._authenticate(headers, tenant)
        requests = _parse_batch_request(body)
        directory, account_ids = self.store.resolve_many(
            tenant, [(r.issuer, r.subject) for r in requests]
        )
        results = []
        for request, account_id in zip(requests, account_ids):
            try:
                resolution = apply_policy(directory, request.issuer, request.subject, account_id)
                results.append(
                    {
                        "issuer": request.issuer,
                        "subject": request.subject,
                        "resolved": True,
                        **resolution.as_dict(),
                    }
                )
            except ServiceError as exc:
                item: dict[str, Any] = {
                    "issuer": request.issuer,
                    "subject": request.subject,
                    "resolved": False,
                    "enabled": False,
                    "revision": directory.revision,
                    "error": {"code": exc.code, "message": exc.message},
                }
                if isinstance(exc.details, dict) and "account_id" in exc.details:
                    item["account_id"] = exc.details["account_id"]
                results.append(item)
        return 200, {
            "tenant": tenant,
            "adapter_id": adapter.adapter_id,
            "revision": directory.revision,
            "all_resolved": all(r["resolved"] for r in results),
            "resolved_count": sum(1 for r in results if r["resolved"]),
            "results": results,
        }


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _parse_identity_request(body: bytes) -> IdentityRequest:
    parsed = loads_strict(body)
    if not isinstance(parsed, dict):
        raise ValidationError("request body must be a JSON object")
    unknown = sorted(set(parsed) - {"issuer", "subject"})
    if unknown:
        raise ValidationError(f"unknown field(s) {unknown!r}; only issuer and subject are accepted")
    for field in ("issuer", "subject"):
        if field not in parsed:
            raise ValidationError(f"missing required field {field!r}")
        value = parsed[field]
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be a string, got {type(value).__name__}")
        if value == "":
            raise ValidationError(f"{field} must be a non-empty string")
        if len(value) > MAX_STRING_CHARS:
            raise ValidationError(f"{field} is longer than {MAX_STRING_CHARS} characters")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValidationError(f"{field} contains unpaired surrogate code points") from None
    return IdentityRequest(parsed["issuer"], parsed["subject"])


def _parse_batch_request(body: bytes) -> list[IdentityRequest]:
    parsed = loads_strict(body)
    if not isinstance(parsed, dict) or set(parsed) != {"identities"}:
        raise ValidationError("batch body must be an object with exactly one field 'identities'")
    raw_identities = parsed["identities"]
    if not isinstance(raw_identities, list):
        raise ValidationError("identities must be an array")
    if not raw_identities:
        raise ValidationError("identities must not be empty")
    if len(raw_identities) > MAX_BATCH_IDENTITIES:
        raise ValidationError(f"identities exceeds {MAX_BATCH_IDENTITIES} entries")
    requests: list[IdentityRequest] = []
    for index, item in enumerate(raw_identities):
        path = f"identities[{index}]"
        if not isinstance(item, dict) or set(item) != {"issuer", "subject"}:
            raise ValidationError(f"{path} must contain exactly issuer and subject")
        issuer, subject = item["issuer"], item["subject"]
        for field, value in (("issuer", issuer), ("subject", subject)):
            if not isinstance(value, str) or value == "":
                raise ValidationError(f"{path}.{field} must be a non-empty string")
            if len(value) > MAX_STRING_CHARS:
                raise ValidationError(f"{path}.{field} is too long")
        requests.append(IdentityRequest(issuer, subject))
    return requests


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def make_handler(application: Application, *, quiet: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "IdentityDirectory/1.0"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            if not quiet:
                sys.stderr.write(
                    "%s - - [%s] %s\n"
                    % (self.client_address[0], self.log_date_time_string(), fmt % args)
                )

        def _read_body(self) -> bytes:
            if self.headers.get("Transfer-Encoding"):
                raise InvalidRequest(
                    "Transfer-Encoding is not supported; send a Content-Length body"
                )
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                return b""
            try:
                length = int(length_header)
            except ValueError:
                raise InvalidRequest("Content-Length must be an integer") from None
            if length < 0:
                raise InvalidRequest("Content-Length must not be negative")
            if length > MAX_BODY_BYTES:
                raise InvalidRequest(f"request body exceeds {MAX_BODY_BYTES} bytes")
            return self.rfile.read(length)

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            try:
                body = self._read_body()
            except ServiceError as exc:
                # The request body is in an unknown state on the socket, so the
                # connection cannot be reused for another request.
                self.close_connection = True
                self._respond(exc.status, {"error": {"code": exc.code, "message": exc.message}})
                return
            try:
                status, payload = application.dispatch(method, self.path, self.headers, body)
            except Exception:  # pragma: no cover - dispatch already guards
                traceback.print_exc(file=sys.stderr)
                status, payload = 500, {"error": {"code": "internal_error", "message": "unexpected error"}}
            try:
                self._respond(status, payload)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - killed peer
                pass

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

    return Handler


def create_server(
    store: Store,
    adapters: AdapterRegistry,
    host: str,
    port: int,
    *,
    quiet: bool = False,
) -> ThreadingHTTPServer:
    application = Application(store, adapters)
    server = ThreadingHTTPServer((host, port), make_handler(application, quiet=quiet))
    server.daemon_threads = True
    return server


def serve(
    store: Store,
    adapters: AdapterRegistry,
    host: str,
    port: int,
    *,
    quiet: bool = False,
) -> None:
    server = create_server(store, adapters, host, port, quiet=quiet)
    try:
        print(
            json.dumps(
                {
                    "event": "listening",
                    "host": host,
                    "port": server.server_address[1],
                    "pid": os.getpid(),
                }
            ),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
