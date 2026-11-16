"""Shared test helpers: server clusters, HTTP client, fixtures, logging.

Design notes
------------
* Tests never call idsvc parsing/query code to compute an expected result.
  Expected mappings are built from literal fixture data with plain dict/list
  logic in this module and in the individual tests.
* Adapter credentials are read from the adapter file only to put them in the
  Authorization header. They are never written to logs, results or assertion
  messages: ``redact`` is applied to every log record.
* Processes are real OS processes; ``kill`` uses ``Popen.kill``.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / ".runtime-tmp" / "test-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ACME = "acme"
GLOBEX = "globex"
ACME_IDP = "https://idp.acme.example/oidc"
ACME_PARTNER = "https://login.partner.example/saml"
GLOBEX_IDP = "https://idp.globex.example/oidc"
GLOBEX_PARTNER = "https://login.partner.example/saml"


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


class TestLog:
    """Append-only JSONL log; every line is redacted of known credentials."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.path = LOG_DIR / f"{name}.log"
        self.path.write_text("", encoding="utf-8")
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register_secret(self, value: str) -> None:
        if value:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        line = self.redact(line)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


# ---------------------------------------------------------------------------
# fixtures (independent of the implementation)
# ---------------------------------------------------------------------------


def account(account_id: str, email: str, display_name: str, enabled: bool = True) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "email": email,
        "display_name": display_name,
        "enabled": enabled,
    }


def identity(issuer: str, subject: str, account_id: str) -> dict[str, Any]:
    return {"issuer": issuer, "subject": subject, "account_id": account_id}


def snapshot(
    tenant: str,
    revision: int,
    accounts: Iterable[dict[str, Any]],
    identities: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    # Deep-copied so a test can mutate its payload without corrupting the
    # module-level fixture lists for later tests.
    return {
        "tenant": tenant,
        "revision": revision,
        "accounts": [dict(a) for a in accounts],
        "identities": [dict(i) for i in identities],
    }


def expected_map(identities: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Reference mapping produced only from fixture identity dicts."""
    mapping: dict[tuple[str, str], str] = {}
    for item in identities:
        key = (item["issuer"], item["subject"])
        if key in mapping:
            raise AssertionError(f"fixture has duplicate identity {key!r}")
        mapping[key] = item["account_id"]
    return mapping


def enabled_map(accounts: Iterable[dict[str, Any]]) -> dict[str, bool]:
    return {a["account_id"]: bool(a["enabled"]) for a in accounts}


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class ConnectionFailed(Exception):
    pass


def http_json(
    base_url: str,
    method: str,
    path: str,
    *,
    body: Any = None,
    raw: bytes | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, Any]]:
    data: bytes | None
    if raw is not None:
        data = raw
    elif body is None:
        data = None
    else:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Connection", "close")
    if token is not None:
        request.add_header("Authorization", "Bearer " + token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"raw": payload}
    except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as exc:
        raise ConnectionFailed(f"{type(exc).__name__}: {exc}") from None


# ---------------------------------------------------------------------------
# adapter file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterInfo:
    adapter_id: str
    tenant: str
    issuer: str
    credential: str


def read_adapters(path: Path) -> dict[str, AdapterInfo]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for entry in data["adapters"]:
        result[entry["adapter_id"]] = AdapterInfo(
            adapter_id=entry["adapter_id"],
            tenant=entry["tenant"],
            issuer=entry["issuer"],
            credential=entry["credential"],
        )
    return result


# ---------------------------------------------------------------------------
# server processes
# ---------------------------------------------------------------------------


class ServerProcess:
    """One `python -m idsvc serve` child process."""

    def __init__(self, env: "Environment", index: int, quiet: bool = True) -> None:
        self.env = env
        self.index = index
        self.proc: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self.stderr_path = env.dir / f"server-{index}.stderr.log"
        self.stdout_path = env.dir / f"server-{index}.stdout.log"
        self.quiet = quiet

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("server has not started")
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 20.0) -> "ServerProcess":
        cmd = [
            sys.executable,
            "-m",
            "idsvc",
            "serve",
            "--db",
            str(self.env.db_path),
            "--adapters",
            str(self.env.adapters_path),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ]
        if self.quiet:
            cmd.append("--quiet")
        stdout_file = open(self.stdout_path, "a", encoding="utf-8")
        stderr_file = open(self.stderr_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=self.env.subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1,
        )
        self._stdout_file = stdout_file
        self._stderr_file = stderr_file
        self._reader = threading.Thread(target=self._pump, name=f"srv-{self.index}-out", daemon=True)
        self._reader.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"server {self.index} exited early: {self._tail_stderr()}")
                continue
            stdout_file.write(line + "\n")
            stdout_file.flush()
            try:
                info = json.loads(line)
            except json.JSONDecodeError:
                continue
            if info.get("event") == "listening":
                self.port = int(info["port"])
                return self
        raise RuntimeError(f"server {self.index} did not become ready: {self._tail_stderr()}")

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))

    def _tail_stderr(self) -> str:
        try:
            text = self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<no stderr>"
        return self.env.log.redact(text[-2000:])

    def kill(self) -> int:
        assert self.proc is not None
        self.proc.kill()
        code = self.proc.wait(timeout=20)
        self._close_files()
        self.proc = None
        return code

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._close_files()
        self.proc = None

    def _close_files(self) -> None:
        for attr in ("_stdout_file", "_stderr_file"):
            handle = getattr(self, attr, None)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, attr, None)
        proc = self.proc
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def wait_for_gate(self, gate: Path, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if gate.exists():
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise AssertionError(
                    f"server {self.index} exited while waiting for gate {gate.name}: "
                    f"{self._tail_stderr()}"
                )
            time.sleep(0.005)
        raise AssertionError(f"gate {gate} was not created within {timeout}s")


# ---------------------------------------------------------------------------
# environment / cluster
# ---------------------------------------------------------------------------


DEFAULT_ADAPTER_SPECS = [
    ("corp-idp-adapter", ACME, ACME_IDP),
    ("partner-idp-adapter", ACME, ACME_PARTNER),
    ("globex-idp-adapter", GLOBEX, GLOBEX_IDP),
    ("globex-partner-adapter", GLOBEX, GLOBEX_PARTNER),
]


class Environment:
    def __init__(self, directory: Path, name: str) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = directory / "directory.db"
        self.adapters_path = directory / "adapters.json"
        self.log = TestLog(name)
        self._init()

    @classmethod
    def for_current_test(cls, case: Any, *, wait: bool = True) -> Path:
        """A fresh directory under .runtime-tmp/run/<timestamp>/<case>."""
        import time as _time
        import unicodedata

        stamp = getattr(cls, "_run_stamp", None)
        if stamp is None:
            stamp = _time.strftime("%Y%m%d-%H%M%S", _time.localtime())
            cls._run_stamp = stamp  # type: ignore[attr-defined]
        raw = f"{case.__class__.__module__}.{case.__class__.__name__}.{case._testMethodName}"
        safe = "".join(
            ch if ch.isalnum() or ch in "-_." else "_" for ch in unicodedata.normalize("NFKD", raw)
        )
        return ROOT / ".runtime-tmp" / "run" / stamp / safe

    def subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _init(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "idsvc",
            "init",
            "--db",
            str(self.db_path),
            "--adapters",
            str(self.adapters_path),
        ]
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=self.subprocess_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"idsvc init failed: {result.stdout}\n{result.stderr}")
        self._adapters = read_adapters(self.adapters_path)
        for adapter in self._adapters.values():
            self.log.register_secret(adapter.credential)

    def adapter(self, adapter_id: str) -> AdapterInfo:
        return self._adapters[adapter_id]

    def token(self, adapter_id: str) -> str:
        return self._adapters[adapter_id].credential

    def write_snapshot(self, name: str, payload: dict[str, Any] | str) -> Path:
        path = self.dir / f"{name}.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


class Cluster:
    """A shared database plus N independent HTTP service processes."""

    def __init__(self, env: Environment) -> None:
        self.env = env
        self.servers: list[ServerProcess] = []

    def start(self, count: int) -> "Cluster":
        for index in range(count):
            server = ServerProcess(self.env, index)
            server.start()
            self.env.log.write(
                {"event": "server_started", "index": index, "pid": server.proc.pid, "port": server.port}
            )
            self.servers.append(server)
        return self

    def __len__(self) -> int:
        return len(self.servers)

    def __getitem__(self, index: int) -> ServerProcess:
        return self.servers[index]

    def stop(self) -> None:
        for server in self.servers:
            try:
                server.stop()
            except Exception:  # pragma: no cover - cleanup best effort
                pass
        self.servers = []

    # -- convenience -----------------------------------------------------

    def publish(self, server_index: int, tenant: str, payload: dict[str, Any],
                headers: dict[str, str] | None = None, timeout: float = 60.0
                ) -> tuple[int, dict[str, Any]]:
        server = self.servers[server_index]
        status, body = http_json(
            server.base_url, "PUT", f"/v1/tenants/{tenant}/directory",
            body=payload, headers=headers, timeout=timeout,
        )
        self.env.log.write(
            {
                "event": "publish",
                "server": server_index,
                "tenant": tenant,
                "revision": payload.get("revision"),
                "status": status,
                "response": body,
            }
        )
        return status, body

    def publish_raw(self, server_index: int, tenant: str, raw: bytes
                    ) -> tuple[int, dict[str, Any]]:
        server = self.servers[server_index]
        status, body = http_json(
            server.base_url, "PUT", f"/v1/tenants/{tenant}/directory", raw=raw
        )
        self.env.log.write(
            {"event": "publish_raw", "server": server_index, "tenant": tenant,
             "body": raw.decode("utf-8", "replace"), "status": status, "response": body}
        )
        return status, body

    def resolve(self, server_index: int, tenant: str, adapter_id: str,
                issuer: str, subject: str) -> tuple[int, dict[str, Any]]:
        server = self.servers[server_index]
        status, body = http_json(
            server.base_url, "POST", f"/v1/tenants/{tenant}/resolve",
            body={"issuer": issuer, "subject": subject},
            token=self.env.token(adapter_id),
        )
        self.env.log.write(
            {
                "event": "resolve",
                "server": server_index,
                "tenant": tenant,
                "adapter_id": adapter_id,
                "identity": {"issuer": issuer, "subject": subject},
                "status": status,
                "response": body,
            }
        )
        return status, body

    def batch(self, server_index: int, tenant: str, adapter_id: str,
              identities: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        server = self.servers[server_index]
        status, body = http_json(
            server.base_url, "POST", f"/v1/tenants/{tenant}/resolve-batch",
            body={"identities": identities}, token=self.env.token(adapter_id),
        )
        self.env.log.write(
            {
                "event": "batch",
                "server": server_index,
                "tenant": tenant,
                "adapter_id": adapter_id,
                "count": len(identities),
                "status": status,
                "revision": body.get("revision"),
                "resolved_count": body.get("resolved_count"),
            }
        )
        return status, body

    def stats(self, server_index: int, tenant: str) -> dict[str, Any]:
        server = self.servers[server_index]
        status, body = http_json(server.base_url, "GET", f"/v1/tenants/{tenant}")
        assert status == 200, f"stats failed: {status} {body}"
        return body


# ---------------------------------------------------------------------------
# publisher subprocesses
# ---------------------------------------------------------------------------


@dataclass
class PublisherResult:
    command: list[str]
    exit_code: int
    result: dict[str, Any] | None
    stdout: str
    stderr: str


def start_publisher(
    env: Environment,
    tenant: str,
    snapshot_path: Path,
    url: str | None,
    *,
    name: str,
    sync_point: str | None = None,
    sync_gate: Path | None = None,
    sync_timeout: float = 60.0,
) -> tuple[subprocess.Popen[str], Path, Path]:
    result_path = env.dir / f"publisher-{name}.result.json"
    stdout_path = env.dir / f"publisher-{name}.stdout.log"
    stderr_path = env.dir / f"publisher-{name}.stderr.log"
    cmd = [
        sys.executable,
        "-m",
        "idsvc",
        "publish",
        "--db",
        str(env.db_path),
        "--tenant",
        tenant,
        "--file",
        str(snapshot_path),
        "--result-file",
        str(result_path),
    ]
    if url is not None:
        cmd += ["--url", url]
    if sync_point:
        if sync_gate is None:
            raise ValueError("sync_point requires sync_gate")
        cmd += ["--sync-point", sync_point, "--sync-gate", str(sync_gate),
                "--sync-timeout", str(sync_timeout)]
    stdout_file = open(stdout_path, "w", encoding="utf-8")
    stderr_file = open(stderr_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env.subprocess_env(),
        stdout=stdout_file,
        stderr=stderr_file,
        text=True,
    )
    # Caller closes the files by waiting on the process; keep references on it.
    proc._idsvc_files = (stdout_file, stderr_file)  # type: ignore[attr-defined]
    proc._idsvc_paths = (stdout_path, stderr_path, result_path, snapshot_path)  # type: ignore[attr-defined]
    return proc, result_path, stderr_path


def finish_publisher(proc: subprocess.Popen[str], result_path: Path, stderr_path: Path) -> PublisherResult:
    exit_code = proc.wait(timeout=120)
    for handle in getattr(proc, "_idsvc_files", ()):
        try:
            handle.close()
        except OSError:
            pass
    stdout_path = getattr(proc, "_idsvc_paths", (None,))[0]
    snapshot_path = getattr(proc, "_idsvc_paths", (None, None, None, None))[3]
    stdout = stdout_path.read_text(encoding="utf-8") if stdout_path else ""
    stderr = stderr_path.read_text(encoding="utf-8") if stderr_path else ""
    result = None
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    return PublisherResult(
        command=list(proc.args) if isinstance(proc.args, list) else [],
        exit_code=exit_code,
        result=result,
        stdout=stdout,
        stderr=stderr,
    )


def snapshot_path_for(env: Environment, name: str, payload: dict[str, Any]) -> Path:
    return env.write_snapshot(name, payload)


def wait_for_file(path: Path, timeout: float = 30.0, *, description: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s waiting for {description or path}")


def release_gate(path: Path) -> None:
    """Delete the gate file, letting the paused process proceed."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class GatePublisher:
    """A real publisher subprocess paused at a sync point.

    ``base_url=None`` starts the CLI publisher against the database directly,
    so the child itself owns the transaction; that is the process to kill for
    a genuine mid-transaction crash. With a ``base_url`` the child is an HTTP
    client and the *server* owns the transaction, so kill the server instead.
    """

    def __init__(self, env: Environment, name: str, tenant: str, payload: dict[str, Any],
                 base_url: str | None, sync_point: str, *, sync_timeout: float = 120.0) -> None:
        self.env = env
        self.name = name
        self.tenant = tenant
        self.payload = payload
        self.base_url = base_url
        self.sync_point = sync_point
        self.gate = env.dir / f"gate-{name}.flag"
        self.snapshot_path = env.write_snapshot(f"gate-{name}", payload)
        self.proc: subprocess.Popen[str] | None = None
        self.result_path = env.dir / f"publisher-{name}.result.json"
        self.stderr_path = env.dir / f"publisher-{name}.stderr.log"
        self._sync_timeout = sync_timeout

    def start(self) -> "GatePublisher":
        self.proc, self.result_path, self.stderr_path = start_publisher(
            self.env,
            self.tenant,
            self.snapshot_path,
            self.base_url,
            name=self.name,
            sync_point=self.sync_point,
            sync_gate=self.gate,
            sync_timeout=self._sync_timeout,
        )
        wait_for_file(self.gate, timeout=30, description=f"gate {self.gate.name}")
        self.env.log.write(
            {
                "event": "gate_reached",
                "name": self.name,
                "pid": self.proc.pid,
                "sync_point": self.sync_point,
                "revision": self.payload["revision"],
                "gate": self.gate.name,
                "mode": "http" if self.base_url else "direct",
            }
        )
        return self

    def release(self) -> PublisherResult:
        release_gate(self.gate)
        assert self.proc is not None
        result = finish_publisher(self.proc, self.result_path, self.stderr_path)
        self.env.log.write(
            {
                "event": "gate_released",
                "name": self.name,
                "revision": self.payload["revision"],
                "exit_code": result.exit_code,
                "result": result.result,
            }
        )
        self.proc = None
        return result

    def kill(self) -> int:
        """Kill the process at the gate (real SIGKILL-equivalent)."""
        assert self.proc is not None
        pid = self.proc.pid
        code = self.proc.kill()
        # Popen.kill() sends the signal; wait to reap.
        code = self.proc.wait(timeout=20)
        for handle in getattr(self.proc, "_idsvc_files", ()):
            try:
                handle.close()
            except OSError:
                pass
        self.env.log.write(
            {
                "event": "gate_killed",
                "name": self.name,
                "pid": pid,
                "sync_point": self.sync_point,
                "revision": self.payload["revision"],
                "wait_status": code,
                "result_file_exists": self.result_path.exists(),
            }
        )
        self.proc = None
        return code

    def wait_finished(self) -> PublisherResult:
        assert self.proc is not None
        result = finish_publisher(self.proc, self.result_path, self.stderr_path)
        self.proc = None
        return result
