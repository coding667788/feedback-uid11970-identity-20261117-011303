# Multi-tenant Identity Directory (post-SSO account mapping)

A from-scratch identity directory service for the step *after* upstream SSO
authentication. It stores explicit, stable mappings from external identities
`(tenant, issuer, subject)` to immutable internal `account_id`s, and it swaps a
whole tenant's directory atomically between published revisions.

Runs on **Windows native Python 3.12, standard library only** (`http.server`,
`sqlite3`, `unittest`), CPU only, with no Docker, no network dependency and no
third-party identity component. Services and clients bind `127.0.0.1`.

* Contract and schema: [`docs/SPEC.md`](docs/SPEC.md)
* Design and consistency argument: [`docs/DESIGN.md`](docs/DESIGN.md)
* Captured demo transcript: [`docs/demo-output.txt`](docs/demo-output.txt)

---

## Quick start (Windows, PowerShell or cmd)

From the project root (the directory containing `idsvc/`):

```bat
:: 1. create the database and a fresh adapter credential file
py -3.12 -m idsvc init --db runtime\directory.db --adapters runtime\adapters.json

:: 2. start four independent service processes sharing that one database
start "ids-1" py -3.12 -m idsvc serve --db runtime\directory.db --adapters runtime\adapters.json --port 8801
start "ids-2" py -3.12 -m idsvc serve --db runtime\directory.db --adapters runtime\adapters.json --port 8802
start "ids-3" py -3.12 -m idsvc serve --db runtime\directory.db --adapters runtime\adapters.json --port 8803
start "ids-4" py -3.12 -m idsvc serve --db runtime\directory.db --adapters runtime\adapters.json --port 8804

:: 3. publish full snapshots
py -3.12 -m idsvc publish --url http://127.0.0.1:8801 --tenant acme  --file examples\snapshots\acme_r3.json
py -3.12 -m idsvc publish --url http://127.0.0.1:8802 --tenant globex --file examples\snapshots\globex_r2.json

:: 4. resolve an external identity through a different process
py -3.12 -m idsvc resolve --url http://127.0.0.1:8803 --tenant acme ^
    --issuer https://idp.acme.example/oidc --subject 00u-as-9011 ^
    --adapters runtime\adapters.json --adapter-id corp-idp-adapter

:: 5. batch resolve in one revision
py -3.12 -m idsvc batch --url http://127.0.0.1:8804 --tenant acme ^
    --file examples\batches\acme_batch.json ^
    --adapters runtime\adapters.json --adapter-id corp-idp-adapter
```

`py -3.12` works with the python.org launcher; if your interpreter is on `PATH`
as `python`, substitute `python`. In Git Bash use `\`-free paths or forward
slashes. On this evaluation machine the interpreter is
`D:/CC_Codex_FeedBack/runtimes/cc005-csv-python312/python.exe`.

If you run the commands from a directory other than the project root, set
`PYTHONPATH` to the project root (or use its absolute path in `cd`); the shell
must be able to import `idsvc`. The `start` command above launches each service
in its own window, which is what makes them four genuinely independent OS
processes. To run them inside one terminal instead, start each `serve` with a
different `--port` in its own terminal.

### One-shot demo

```bat
py -3.12 examples\demo.py
```

This creates a throw-away database under `.runtime-tmp\demo`, starts four
service processes, and walks through publication, exact-triple resolution,
cross-tenant isolation, batch reads, a binding swap, a rejected snapshot,
idempotency/stale/conflict, and a deny-all switch. It never prints credentials.

### Tests

```bat
py -3.12 tests\run_all.py
```

93 tests, ~70 s. Writes `.runtime-tmp\test-logs\summary.{json,log}` (per-test
outcome, actual request results, final published revision per test database)
and one JSONL request log per test. The runner also audits every produced
artifact for adapter credential strings and fails if one appears. The 4-server
tests use the runtime interpreter's `sys.executable` and real child processes.

---

## Commands

| Command | Purpose |
|---|---|
| `init` | create schema (WAL), write adapter credentials if absent. Never rotates existing credentials unless `--reset-adapters`. |
| `serve` | one HTTP service process; refuses to start on an uninitialised database |
| `publish` | `PUT` a full snapshot, via `--url` (HTTP) or directly on the DB |
| `resolve` | resolve one `(issuer, subject)`; credential from `--adapters`/`--adapter-id` |
| `batch` | resolve a JSON list of identities in one revision |
| `show` / `bindings` / `history` / `account` / `tenants` | inspect the current revision |

All commands take `--db`. `publish` also accepts the failure-injection options
`--sync-point pre-commit|post-commit`, `--sync-gate <file>`, `--sync-timeout`
and `--result-file`, used by the crash tests.

## HTTP API

| Method | Path |
|---|---|
| GET | `/healthz` |
| GET | `/v1/tenants` |
| GET | `/v1/tenants/{tenant}` |
| GET | `/v1/tenants/{tenant}/accounts/{account_id}` |
| GET | `/v1/tenants/{tenant}/bindings` |
| GET | `/v1/tenants/{tenant}/history` |
| PUT | `/v1/tenants/{tenant}/directory` |
| POST | `/v1/tenants/{tenant}/resolve` |
| POST | `/v1/tenants/{tenant}/resolve-batch` |

`resolve` and `resolve-batch` need `Authorization: Bearer <adapter credential>`.

Malformed JSON: `400`. Schema violation: `422` (`invalid_snapshot`). Bad or
wrong-tenant credential: `401`. Unbound or disabled identity: `403`
(`identity_not_resolvable`). Older revision: `409 stale_revision`. Same
revision, different content: `409 revision_conflict`. Unknown tenant: `404`.
Database busy beyond the 15 s busy timeout: `503 store_busy`.

## Snapshot format

```json
{
  "tenant": "acme",
  "revision": 4,
  "accounts": [
    {"account_id": "acme-1001", "email": "ana.reyes@acme.example",
     "display_name": "Ana Reyes", "enabled": true}
  ],
  "identities": [
    {"issuer": "https://idp.acme.example/oidc", "subject": "00u-as-9011",
     "account_id": "acme-1001"}
  ]
}
```

`identities` may be spelled `bindings` or `external_identities` (exactly one).
Full rules: [`docs/SPEC.md` §2–3](docs/SPEC.md).

## What is and is not implemented

Implemented: strict whole-snapshot schema validation; per-tenant isolation;
exact `(tenant, issuer, subject)` matching; durable revision bookkeeping;
atomic publication across processes; batch reads pinned to one revision;
three controllable sync points; real process-kill recovery tests; audit that no
credential is logged.

Deliberately out of scope (per the brief): token refresh, password login,
calling any external identity provider, and multi-node replication beyond a
single shared SQLite file.

---

## 中文摘要

实现要点：

* **身份绑定**：账号以不可变 `account_id` 标识，邮箱/显示名可改且从不用于匹配。
  外部身份严格按 `(tenant, issuer, subject)` 原始字符串精确匹配，不做大小写、
  去空白或 URL 归一化。每个租户目录完全隔离。仅预置测试适配器凭据可调用
  resolve，凭据绑定租户；未绑定或已停用账号返回 403，绝不因邮箱相同而落到
  其他账号。
* **整版快照**：`revision`、完整账号集合、完整绑定集合一次性提交；校验失败整版
  拒绝（422），先校验后开事务，不可能留下部分修改。高版本可发布，低版本
  `stale`（409），同版本等价为幂等成功（200），同版本不同内容为 `conflict`
  （409）。等价性忽略键序与两个集合的排列，保留字符串内容、类型与启用状态。
  绑定互换在同一事务内整体替换，不会触发中间唯一性冲突。
* **并发与一致性**：正确性完全依赖 SQLite（`BEGIN IMMEDIATE`、主键约束、
  WAL 读快照），不依赖进程内锁。至少 4 个独立服务进程共享一个数据库；
  批量 resolve 一次请求只读取一个已发布版本；发布响应前已提交，故响应后
  的查询必见该版或更新版。
* **故障注入**：`pre-open` / `pre-commit` / `post-commit` 三个可控同步点，
  测试用真实 `Popen.kill` 终止自己启动的进程（提交前与提交后两处均已覆盖，
  且分别覆盖直连发布进程与服务进程），重启服务后读取目录并重试原快照，
  验证原子性、幂等性与版本不倒退。
* **测试**：93 个用例（schema、存储、HTTP 功能、并发、进程终止、CLI），
  覆盖相同邮箱不合并、三元组精确匹配、停用与移除、合法互换、无效回滚、
  同版等价/冲突、不同版本竞争、批量与发布交错、两处进程终止恢复。日志记录
  真实请求结果与最终版本，并自动审计确认任何日志/数据库都不含适配器凭据。

运行 `python tests\run_all.py` 执行全部测试；`python examples\demo.py`
运行四个服务进程的端到端演示。
