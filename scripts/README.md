# Script map

## Active v2 operations

- `runtime_paths.ps1`: resolve the per-user, host-local secret path outside the repository.
- `prepare_signoz_secret.ps1`: atomically create or migrate local bootstrap secrets and restrict them to the current user/SYSTEM/administrators on Windows (mode `0600` on POSIX).
- `bootstrap_signoz_access.py`: provision/validate a local viewer service-account key.
- `product_run.py`: current-lineage preflight by default, hard exit, fresh recovery, projection, export, selective exact query-back, and product v2 document under one absolute deadline. The default/max is 90 seconds; exit `74` reports `product_deadline_exceeded`, and deadline or product-stage failures retain a typed `failure.json` after run-directory creation. `--candidate-inputs` is an explicit release-authoring mode only. Its bounded `--source` records `direct-product-run`, `local-reproduction`, or `web-action`; callers set it explicitly.
- `reproduce.ps1` and `reproduce.py`: one-command local stack and fresh proof after a current lineage has been activated. PowerShell bounds environment sync, stack start, shared health readiness, access bootstrap, and the product child, retaining `receipts/work/reproduce-setup-latest.json`; `reproduce.py` passes the 90-second product budget explicitly and applies a 100-second caller hard stop. `-CandidateInputs`/`--candidate-inputs` is reserved for building a candidate release and is never used by the normal web action.
- `create_lineage.py` and `finalize_lineage.py`: record hash-bound inputs, validate the ordered evidence chain, and exclusively activate a lineage. They refuse overwrites, but the local filesystem is not an immutable store and the current pointer cannot yet be rotated or rolled back.
- `materialize_web_data.py`: validate and copy a registered v2 product run into the retained web proof.
- `fetch_foundry.py`: integrity-check the pinned local Foundry CLI archive.

The full candidate-to-current order, including all three `verify/v2_phase.py` invocations and the post-activation semantic audit, is in `verify/README.md`.

## CLI evidence levels

The packaged CLI resolves the project from the current directory or an explicit global option placed before the subcommand, for example `relay-otel --project-root C:\path\to\poc-relay-otel doctor`. It validates `pyproject.toml` and the deployment pins instead of relying on the source checkout location baked into the wheel.

- `relay-otel project` and its `export` alias require a valid current lineage. They create a WAL-consistent SQLite snapshot under the project root, revalidate the lineage after snapshotting, and bind the resulting projection/export to both the lineage and snapshot hash. Their outputs are suitable inputs to a current-lineage evidence workflow, subject to the documented local-only trust boundary.
- `relay-otel diff` reads the path supplied by the operator and compares aware/control projections for diagnosis. It does not create a lineage-bound snapshot and must not be presented as registered or current evidence.
