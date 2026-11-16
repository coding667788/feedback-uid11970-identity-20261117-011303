"""Run the full automated test suite and write a machine-readable summary.

Usage (Windows, from the project root):

    python tests/run_all.py

Outputs:
  .runtime-tmp/test-logs/summary.json   per-test outcome, duration, log file
  .runtime-tmp/test-logs/summary.log    human-readable summary with final versions
  .runtime-tmp/test-logs/<test>.log     JSONL request/result log per test

No adapter credential is written anywhere: TestLog redacts registered secrets,
and the summary never includes request headers.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.support import LOG_DIR  # noqa: E402

MODULES = [
    "tests.test_schema",
    "tests.test_store",
    "tests.test_functional",
    "tests.test_concurrency",
    "tests.test_process_kill",
    "tests.test_cli",
]


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.records: list[dict] = []
        self._started: dict[str, float] = {}

    def startTest(self, test) -> None:  # noqa: N802
        self._started[test.id()] = time.monotonic()
        super().startTest(test)

    def addSuccess(self, test) -> None:  # noqa: N802
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err) -> None:  # noqa: N802
        super().addFailure(test, err)
        self._record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err) -> None:  # noqa: N802
        super().addError(test, err)
        self._record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def _record(self, test, status: str, detail: str = "") -> None:
        started = self._started.get(test.id(), time.monotonic())
        self.records.append(
            {
                "test": test.id(),
                "status": status,
                "duration_s": round(time.monotonic() - started, 3),
                "detail": detail[-4000:] if detail else "",
            }
        )


def final_versions(*, since: float) -> list[dict]:
    """Read the current revision of every tenant from test databases written
    during this run.

    Uses sqlite3 directly rather than idsvc so that the summary is produced by
    logic independent of the code under test.
    """
    import sqlite3

    versions = []
    run_root = ROOT / ".runtime-tmp" / "run"
    if not run_root.exists():
        return versions
    for db in sorted(run_root.glob("*/*/directory.db")):
        try:
            if db.stat().st_mtime < since - 1:
                continue
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except (OSError, sqlite3.Error):
            continue
        try:
            rows = conn.execute(
                "SELECT tenant, current_revision FROM tenants ORDER BY tenant"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        if rows:
            versions.append(
                {
                    "test": db.parent.name,
                    "tenants": [{"tenant": t, "revision": r} for t, r in rows],
                }
            )
    return versions


def credential_leak_audit() -> list[str]:
    """Scan every artifact this run produced for adapter credential strings.

    The adapter files themselves are excluded (they are the secret store), as
    is the directory database (which never stores credentials and is checked
    separately). Everything else -- service stdout/stderr, publisher logs,
    per-test JSONL request logs, the summary itself -- must be clean.
    """
    import json as _json
    import sqlite3

    findings: list[str] = []
    run_root = ROOT / ".runtime-tmp" / "run"
    credential_by_dir: dict[Path, list[str]] = {}
    if run_root.exists():
        for adapters in run_root.glob("*/*/adapters.json"):
            data = _json.loads(adapters.read_text(encoding="utf-8"))
            credential_by_dir[adapters.parent] = [
                entry["credential"] for entry in data.get("adapters", [])
            ]

    for directory, credentials in credential_by_dir.items():
        for path in directory.rglob("*"):
            if not path.is_file() or path.name == "adapters.json":
                continue
            if path.suffix in (".db", ".db-wal", ".db-shm"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for credential in credentials:
                if credential and credential in text:
                    findings.append(f"{path}: contains an adapter credential")

        db = directory / "directory.db"
        if db.exists():
            try:
                conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
                try:
                    dump = "\n".join(conn.iterdump())
                finally:
                    conn.close()
            except sqlite3.Error:
                dump = ""
            for credential in credentials:
                if credential and credential in dump:
                    findings.append(f"{db}: credential persisted in database")

    for path in LOG_DIR.rglob("*"):
        if not path.is_file() or path.name in ("summary.json", "summary.log"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for credentials in credential_by_dir.values():
            for credential in credentials:
                if credential and credential in text:
                    findings.append(f"{path}: contains an adapter credential")
    return findings


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in MODULES:
        suite.addTests(loader.loadTestsFromName(module))

    runner = unittest.TextTestRunner(
        verbosity=2, resultclass=RecordingResult, stream=sys.stdout
    )
    started_epoch = time.time()
    started = time.monotonic()
    result = runner.run(suite)
    elapsed = time.monotonic() - started
    leaks = credential_leak_audit()

    summary = {
        "total": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "duration_s": round(elapsed, 2),
        "started_local": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started_epoch)),
        "started_epoch": started_epoch,
        "tests": result.records,
        "final_versions": final_versions(since=started_epoch),
        "credential_leaks": leaks,
    }
    (LOG_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        f"Identity directory test suite — {summary['started_local']}",
        f"{summary['total']} tests in {summary['duration_s']}s: "
        f"{summary['failures']} failures, {summary['errors']} errors, "
        f"{summary['skipped']} skipped",
        "",
        "Per-test outcome:",
    ]
    for record in result.records:
        lines.append(
            f"  [{record['status']:7}] {record['test']} ({record['duration_s']}s)"
        )
        if record["status"] not in ("passed",):
            for line in record["detail"].splitlines():
                lines.append(f"           | {line}")
    lines.append("")
    lines.append("Final published revisions per test:")
    for entry in summary["final_versions"]:
        tenants = ", ".join(f"{t['tenant']}={t['revision']}" for t in entry["tenants"])
        lines.append(f"  {entry['test']}: {tenants}")
    lines.append("")
    lines.append("Credential leak audit:")
    if summary["credential_leaks"]:
        for finding in summary["credential_leaks"]:
            lines.append(f"  LEAK {finding}")
    else:
        lines.append("  clean (no adapter credential in any log, result or database)")
    lines.append("")
    lines.append(f"Detailed request logs: {LOG_DIR}")
    (LOG_DIR / "summary.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return 0 if result.wasSuccessful() and not leaks else 1


if __name__ == "__main__":
    sys.exit(main())
