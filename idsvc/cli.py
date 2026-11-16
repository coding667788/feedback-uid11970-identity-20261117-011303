"""Command-line entry point: init, serve, publish, resolve, batch, inspect.

Every command accepts ``--db`` for the SQLite file. publish/resolve/batch can
either talk to a running HTTP server (``--url``) or operate on the database
directly through the same transaction code the server uses; the HTTP path is
the one to use whenever concurrency matters.

The adapter credential is read from the adapter file and is never printed,
logged or included in error output.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from .adapters import AdapterRegistry, generate_adapter_file
from .api import serve
from .errors import ServiceError
from .policy import apply_policy
from .snapshot import load_snapshot_file, loads_strict, validate_snapshot
from .store import Store

DEFAULT_ADAPTER_SPECS = [
    {"adapter_id": "corp-idp-adapter", "tenant": "acme", "issuer": "https://idp.acme.example/oidc"},
    {"adapter_id": "partner-idp-adapter", "tenant": "acme", "issuer": "https://login.partner.example/saml"},
    {"adapter_id": "globex-idp-adapter", "tenant": "globex", "issuer": "https://idp.globex.example/oidc"},
    {"adapter_id": "globex-partner-adapter", "tenant": "globex", "issuer": "https://login.partner.example/saml"},
]


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def _http(method: str, base_url: str, path: str, *, body: dict[str, Any] | None = None,
          token: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover
            return exc.code, {"error": {"code": "non_json_response", "message": raw}}


def _credential(adapter_file: str, adapter_id: str) -> str:
    with open(adapter_file, "rb") as handle:
        raw = json.loads(handle.read().decode("utf-8"))
    for entry in raw["adapters"]:
        if entry["adapter_id"] == adapter_id:
            return entry["credential"]
    raise SystemExit(f"adapter {adapter_id!r} is not present in {adapter_file}")


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    create_adapters = not os.path.exists(args.adapters) or args.reset_adapters
    if create_adapters:
        public = generate_adapter_file(args.adapters, DEFAULT_ADAPTER_SPECS)
    else:
        public = AdapterRegistry.load(args.adapters).public_list()
    store = _store(args)
    store.initialize()
    _emit(
        {
            "database": store.path,
            "adapter_file": os.path.abspath(args.adapters),
            "schema_version": store.schema_version(),
            "adapters": public,
            "credentials_written": create_adapters,
            "next": [
                "python -m idsvc serve --db <db> --adapters <adapter file> --port 8801",
                "python -m idsvc publish --url http://127.0.0.1:8801 --tenant acme --file examples/snapshots/acme_r3.json",
            ],
        }
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    store = _store(args)
    if store.schema_version() is None:
        _emit(
            {
                "error": {
                    "code": "database_not_initialised",
                    "message": f"database {store.path!r} is not initialised; "
                               "run 'python -m idsvc init' first",
                }
            }
        )
        return 2
    adapters = AdapterRegistry.load(args.adapters)
    serve(store, adapters, args.host, args.port, quiet=args.quiet)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    if args.sync_point:
        if not args.sync_gate:
            raise SystemExit("--sync-point requires --sync-gate")
        if not (0 < args.sync_timeout <= 3600):
            raise SystemExit("--sync-timeout must be within (0, 3600]")
    snapshot = load_snapshot_file(args.file, tenant=args.tenant)
    result: dict[str, Any]
    exit_code = 0
    if args.url:
        with open(args.file, "rb") as handle:
            body = loads_strict(handle.read())
        headers: dict[str, str] = {}
        if args.sync_point:
            headers["X-Sync-Point"] = args.sync_point
            gate = os.path.abspath(args.sync_gate)
            # HTTP header values are Latin-1; the workspace path may not be.
            if all(ord(ch) < 128 for ch in gate):
                headers["X-Sync-Gate"] = gate
            else:
                headers["X-Sync-Gate-B64"] = base64.b64encode(gate.encode("utf-8")).decode("ascii")
            headers["X-Sync-Timeout"] = str(args.sync_timeout)
        try:
            status, payload = _http(
                "PUT", args.url, f"/v1/tenants/{args.tenant}/directory", body=body, headers=headers
            )
            result = {"http_status": status, "response": payload}
            exit_code = 0 if status in (200, 201) else 1
        except (urllib.error.URLError, ConnectionError, http.client.HTTPException, OSError) as exc:
            # The service died or dropped the connection before answering; the
            # publish outcome is unknown to this client. Nothing is invented.
            result = {
                "http_status": None,
                "outcome": "connection_error",
                "message": type(exc).__name__,
            }
            exit_code = 3
    else:
        from .api import _wait_for_gate

        hooks: dict[str, Any] = {}
        if args.sync_point:
            gate = os.path.abspath(args.sync_gate)

            def hook(_kind: str) -> None:
                _wait_for_gate(gate, args.sync_timeout, args.sync_point)

            name = args.sync_point.replace("-", "_") + "_hook"
            hooks[name] = hook
        outcome = _store(args).publish(snapshot, **hooks)
        result = {"http_status": None, "response": outcome.as_dict()}
    if args.result_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.result_file)), exist_ok=True)
        with open(args.result_file, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    _emit(result)
    return exit_code


def cmd_resolve(args: argparse.Namespace) -> int:
    if args.token:
        token = args.token
    else:
        token = _credential(args.adapters, args.adapter_id)
    body = {"issuer": args.issuer, "subject": args.subject}
    if args.url:
        status, payload = _http(
            "POST", args.url, f"/v1/tenants/{args.tenant}/resolve", body=body, token=token
        )
        _emit({"http_status": status, "response": payload})
        return 0 if status == 200 else 1
    store = _store(args)
    directory, account_id = store.resolve_one(args.tenant, args.issuer, args.subject)
    resolution = apply_policy(directory, args.issuer, args.subject, account_id)
    _emit({"http_status": None, "tenant": args.tenant, **resolution.as_dict()})
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    with open(args.file, "rb") as handle:
        body = loads_strict(handle.read())
    if not isinstance(body, dict) or set(body) != {"identities"}:
        raise SystemExit("batch file must be an object with exactly one key 'identities'")
    token = args.token or _credential(args.adapters, args.adapter_id)
    if args.url:
        status, payload = _http(
            "POST", args.url, f"/v1/tenants/{args.tenant}/resolve-batch", body=body, token=token
        )
        _emit({"http_status": status, "response": payload})
        return 0
    store = _store(args)
    wanted = [(i["issuer"], i["subject"]) for i in body["identities"]]
    directory, account_ids = store.resolve_many(args.tenant, wanted)
    results = []
    for (issuer, subject), account_id in zip(wanted, account_ids):
        try:
            resolution = apply_policy(directory, issuer, subject, account_id)
            results.append({"issuer": issuer, "subject": subject, "resolved": True, **resolution.as_dict()})
        except ServiceError as exc:
            results.append(
                {
                    "issuer": issuer,
                    "subject": subject,
                    "resolved": False,
                    "enabled": False,
                    "revision": directory.revision,
                    "error": {"code": exc.code, "message": exc.message},
                }
            )
    _emit({"http_status": None, "tenant": args.tenant, "revision": directory.revision,
           "results": results})
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(args)
    _emit(store.stats(args.tenant))
    return 0


def cmd_bindings(args: argparse.Namespace) -> int:
    _emit(_store(args).bindings(args.tenant))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    _emit({"tenant": args.tenant, "revisions": _store(args).history(args.tenant)})
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    _emit(_store(args).account(args.tenant, args.account_id))
    return 0


def cmd_tenants(args: argparse.Namespace) -> int:
    _emit({"tenants": _store(args).list_tenants()})
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idsvc",
        description="Multi-tenant identity directory with atomic whole-snapshot publication.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, need_db: bool = True) -> None:
        if need_db:
            p.add_argument("--db", default=os.environ.get("IDSVC_DB", "runtime/directory.db"),
                           help="SQLite database file (default: %(default)s)")

    init = sub.add_parser("init", help="create the database and a fresh adapter credential file")
    add_common(init)
    init.add_argument("--adapters", default="runtime/adapters.json",
                      help="adapter credential file (default: %(default)s)")
    init.add_argument("--reset-adapters", action="store_true",
                      help="regenerate credentials even if the adapter file exists")
    init.set_defaults(func=cmd_init)

    serve_p = sub.add_parser("serve", help="run one HTTP service process")
    add_common(serve_p)
    serve_p.add_argument("--adapters", default="runtime/adapters.json")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8801)
    serve_p.add_argument("--quiet", action="store_true", help="suppress per-request log lines")
    serve_p.set_defaults(func=cmd_serve)

    publish = sub.add_parser("publish", help="publish a full tenant snapshot (PUT)")
    add_common(publish)
    publish.add_argument("--tenant", required=True)
    publish.add_argument("--file", required=True)
    publish.add_argument("--url", help="if set, publish over HTTP instead of directly")
    publish.add_argument("--sync-point", choices=("pre-open", "pre-commit", "post-commit"),
                         help="pause at a failure-injection point (tests)")
    publish.add_argument("--sync-gate", default="",
                         help="gate file that the harness deletes to release the pause")
    publish.add_argument("--sync-timeout", type=float, default=120.0,
                         help="max seconds to wait at the sync point (default: %(default)s)")
    publish.add_argument("--result-file", help="write the request result JSON here")
    publish.set_defaults(func=cmd_publish)

    resolve = sub.add_parser("resolve", help="resolve one external identity")
    add_common(resolve)
    resolve.add_argument("--tenant", required=True)
    resolve.add_argument("--issuer", required=True)
    resolve.add_argument("--subject", required=True)
    resolve.add_argument("--adapters", default="runtime/adapters.json")
    resolve.add_argument("--adapter-id", default="corp-idp-adapter")
    resolve.add_argument("--token", help="explicit credential (prefer --adapters)")
    resolve.add_argument("--url", help="if set, resolve over HTTP instead of directly")
    resolve.set_defaults(func=cmd_resolve)

    batch = sub.add_parser("batch", help="resolve many external identities in one revision")
    add_common(batch)
    batch.add_argument("--tenant", required=True)
    batch.add_argument("--file", required=True, help="JSON object {'identities': [...]}")
    batch.add_argument("--adapters", default="runtime/adapters.json")
    batch.add_argument("--adapter-id", default="corp-idp-adapter")
    batch.add_argument("--token")
    batch.add_argument("--url")
    batch.set_defaults(func=cmd_batch)

    show = sub.add_parser("show", help="show the current revision and counts")
    add_common(show)
    show.add_argument("--tenant", required=True)
    show.set_defaults(func=cmd_show)

    bindings = sub.add_parser("bindings", help="list current bindings")
    add_common(bindings)
    bindings.add_argument("--tenant", required=True)
    bindings.set_defaults(func=cmd_bindings)

    history = sub.add_parser("history", help="list stored revisions")
    add_common(history)
    history.add_argument("--tenant", required=True)
    history.set_defaults(func=cmd_history)

    account = sub.add_parser("account", help="show one account and its bindings")
    add_common(account)
    account.add_argument("--tenant", required=True)
    account.add_argument("--account-id", required=True)
    account.set_defaults(func=cmd_account)

    tenants = sub.add_parser("tenants", help="list tenants with a published directory")
    add_common(tenants)
    tenants.set_defaults(func=cmd_tenants)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ServiceError as exc:
        _emit({"error": {"code": exc.code, "message": exc.message,
                         "details": exc.details, "status": exc.status}})
        return 1
    except FileNotFoundError as exc:
        _emit({"error": {"code": "file_not_found", "message": str(exc)}})
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
