"""Runnable end-to-end demo over real HTTP (Windows, stdlib only).

    python examples/demo.py

Starts four independent service processes sharing one SQLite database, then
walks through publication, resolution, swaps, rejections, ordering and batch
reads. Prints request outcomes and final versions. Never prints adapter
credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime-tmp" / "demo"
DB = RUNTIME / "directory.db"
ADAPTERS = RUNTIME / "adapters.json"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "idsvc", *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )


def http(method: str, url: str, path: str, *, body=None, token=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Connection", "close")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def token_for(adapter_id: str) -> str:
    data = json.loads(ADAPTERS.read_text(encoding="utf-8"))
    for entry in data["adapters"]:
        if entry["adapter_id"] == adapter_id:
            return entry["credential"]
    raise SystemExit(f"no adapter {adapter_id}")


def publish(url: str, tenant: str, filename: str):
    payload = json.loads((ROOT / "examples" / "snapshots" / filename).read_text(encoding="utf-8"))
    return http("PUT", url, f"/v1/tenants/{tenant}/directory", body=payload)


def resolve(url: str, tenant: str, adapter_id: str, issuer: str, subject: str):
    return http(
        "POST", url, f"/v1/tenants/{tenant}/resolve",
        body={"issuer": issuer, "subject": subject}, token=token_for(adapter_id),
    )


def show(label: str, status: int, payload: dict) -> None:
    print(f"\n### {label}\nHTTP {status}\n{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}")


def main() -> int:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True)

    print("## 1. initialise database + adapter credentials")
    init = run("init", "--db", str(DB), "--adapters", str(ADAPTERS))
    if init.returncode != 0:
        print(init.stdout, init.stderr)
        return 1
    init_payload = json.loads(init.stdout)
    print(json.dumps({k: init_payload[k] for k in ("database", "adapter_file", "adapters")},
                     ensure_ascii=False, indent=2))

    print("\n## 2. start four independent service processes")
    servers: list[subprocess.Popen[str]] = []
    urls: list[str] = []
    try:
        for index in range(4):
            proc = subprocess.Popen(
                [sys.executable, "-m", "idsvc", "serve", "--db", str(DB),
                 "--adapters", str(ADAPTERS), "--host", "127.0.0.1", "--port", "0", "--quiet"],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            servers.append(proc)
            line = proc.stdout.readline()  # type: ignore[union-attr]
            info = json.loads(line)
            urls.append(f"http://127.0.0.1:{info['port']}")
            print(f"  server {index}: pid={info['pid']} url={urls[-1]}")

        print("\n## 3. publish acme revision 3 and globex revision 2")
        show("publish acme r3", *publish(urls[0], "acme", "acme_r3.json"))
        show("publish globex r2", *publish(urls[1], "globex", "globex_r2.json"))

        print("\n## 4. resolve through different processes")
        show(
            "acme: OIDC subject -> acme-1001",
            *resolve(urls[2], "acme", "corp-idp-adapter",
                     "https://idp.acme.example/oidc", "00u-as-9011"),
        )
        show(
            "same email, different account: partner subject -> acme-1003",
            *resolve(urls[3], "acme", "partner-idp-adapter",
                     "https://login.partner.example/saml", "shared.mailbox"),
        )
        show(
            "globex: same issuer+subject as acme, resolves to globex-1001",
            *resolve(urls[0], "globex", "globex-partner-adapter",
                     "https://login.partner.example/saml", "AS-9011"),
        )
        show(
            "exact triple only: trailing space in subject is NOT bound",
            *resolve(urls[1], "acme", "corp-idp-adapter",
                     "https://idp.acme.example/oidc", "00u-as-9011 "),
        )

        print("\n## 5. batch resolve (one revision for the whole response)")
        batch = json.loads((ROOT / "examples" / "batches" / "acme_batch.json")
                           .read_text(encoding="utf-8"))
        show("batch", *http("POST", urls[2], "/v1/tenants/acme/resolve-batch",
                            body=batch, token=token_for("corp-idp-adapter")))

        print("\n## 6. publish r4: swap two bindings, rename email, disable an account")
        show("publish acme r4 (swap)", *publish(urls[1], "acme", "acme_r4_swap.json"))
        show(
            "swapped: 00u-bo-4412 moved from acme-1002 to acme-1001",
            *resolve(urls[0], "acme", "corp-idp-adapter",
                     "https://idp.acme.example/oidc", "00u-bo-4412"),
        )
        show(
            "swapped onto a disabled account: 00u-as-9011 -> acme-1002 (disabled)",
            *resolve(urls[2], "acme", "corp-idp-adapter",
                     "https://idp.acme.example/oidc", "00u-as-9011"),
        )

        print("\n## 7. invalid snapshot (dangling binding) is rejected whole")
        show("publish invalid", *publish(urls[3], "acme", "acme_invalid_dangling_binding.json"))
        show("revision after rejection", *http("GET", urls[0], "/v1/tenants/acme"))

        print("\n## 8. idempotency / stale / conflict")
        show("republish r4 identical (different array order is equivalent)", *publish(urls[2], "acme", "acme_r4_swap.json"))
        show("republish r3 (stale)", *publish(urls[0], "acme", "acme_r3.json"))
        conflicting = json.loads((ROOT / "examples" / "snapshots" / "acme_r4_swap.json")
                                 .read_text(encoding="utf-8"))
        conflicting["accounts"][2]["display_name"] = "Edited differently"
        show("publish r4 with different content (conflict)",
             *http("PUT", urls[1], "/v1/tenants/acme/directory", body=conflicting))

        print("\n## 9. publish r5: deny-all; batch must be wholly from one revision")
        show("publish acme r5 (deny all)", *publish(urls[3], "acme", "acme_r5_deny_all.json"))
        show("batch after deny-all",
             *http("POST", urls[0], "/v1/tenants/acme/resolve-batch",
                   body=batch, token=token_for("corp-idp-adapter")))

        print("\n## 10. final state read from every server")
        for index, url in enumerate(urls):
            status, body = http("GET", url, "/v1/tenants/acme")
            print(f"  server {index}: HTTP {status} revision={body.get('revision')} "
                  f"accounts={body.get('account_count')} bindings={body.get('identity_count')}")
        print("\nDemo complete. Database:", DB)
        return 0
    finally:
        for proc in servers:
            proc.terminate()
        for proc in servers:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for proc in servers:
            if proc.stdout is not None:
                proc.stdout.close()


if __name__ == "__main__":
    sys.exit(main())
