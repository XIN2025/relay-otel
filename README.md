# relay-otel

What your traces say happened, next to what really happened.

A refund workflow is killed the instant the payment provider answers, then
resumed in a fresh process. The durable journal is the ground truth. From it,
relay-otel builds two OpenTelemetry views of the same run, exports both, and
reads them back from a local SigNoz to confirm SigNoz holds exactly what was
sent.

This is an independent project, not affiliated with SigNoz.
It does not audit SigNoz code.

## The result

| Ground truth (journal) | Aware view | Replay-blind view |
| --- | --- | --- |
| Two attempts at the refund step | Two activation spans | Two activation spans |
| One call to the payment provider | One `effect.execute` | One `effect.execute` |
| One result read back from the journal | One linked `effect.resolve` | A second `effect.execute` |
| One refund | One business result | The same refund counted twice |

The replay-blind view is a deliberate control: it shows what an observer loses
when it cannot tell a real call from a result replayed out of the journal. It is
not a claim that every OpenTelemetry integration double-counts.

## Run it

Needs Windows PowerShell, Docker Desktop (Linux containers), `uvx`, Node 22 and
pnpm 11.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\reproduce.ps1 -Seed 23
```

That syncs the locked Python environment, starts a pinned SigNoz on loopback,
provisions a read-only query key outside the repository, runs the crash and the
fresh resume, exports both views and requires an exact query-back. It only runs
against an activated release (see below) and exits `78` otherwise.

The web workbench:

```powershell
cd web; corepack pnpm install --frozen-lockfile; pnpm verify; pnpm start -- --port 3010
```

Open `http://127.0.0.1:3010`. **Crash and resume** stays disabled until the
release, Python runtime, collector, SigNoz and the query key are all ready.

## Deploy the evidence site

`pnpm build:evidence` writes a static site to `web/out/` with the retained run,
no API routes and the live action disabled. It needs no Python, Docker or
credentials. On Vercel: root directory `web`, build command
`pnpm build:evidence`, output directory `out`.

## Releases

Every source file, config file and top-level Markdown file is hash-bound to the
active release in `receipts/current-lineage.json`. A release is built in three
phases (runtime checks, a live SigNoz integration run, publication checks
including dependency audits and a browser pass), then finalized. The steps are
in [verify/README.md](verify/README.md). CI runs the release audit, so changing a
bound file without cutting a new release turns CI red.

```powershell
.\.venv\Scripts\python.exe verify\release_audit.py
.\.venv\Scripts\python.exe verify\runtime_correctness.py
```

## Repository map

| Path | Purpose |
| --- | --- |
| `src/relay/` | Durable graph runtime, journal, effect recovery |
| `src/relay_otel/` | Projection, OTLP export, SigNoz query-back, release validation |
| `web/` | Loopback-only workbench and static evidence site |
| `deploy/` | Digest-pinned local SigNoz and ClickHouse stack |
| `scripts/` | Reproduction, access bootstrap, release authoring |
| `verify/` | Runtime verifier, release phases and release audit |
| `receipts/lineages/` | The active release's hash-bound evidence |


## Limits

- Single node, one SQLite journal. No leases, no worker pool.
- The payment provider is a local stub; the crash is `os._exit(9)` at a fixed
  point.
- Only local SigNoz on loopback is supported, with a demo-grade key that is not
  rotated.
- Releases are one-shot: there is no pointer rotation or rollback command.
