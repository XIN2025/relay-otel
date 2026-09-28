# Verification map

This directory contains bounded release verifiers, not a conventional generated test suite.

## Active v2

- `runtime_correctness.py`: real hard-exit/fresh-process and lifecycle edge-case verifier.
- `v2_phase.py`: creates the ordered runtime, integration, and publication evidence/receipt pairs for one candidate lineage. It is the only active phase-receipt writer.
- `release_audit.py`: recomputes the complete active lineage selected by `receipts/current-lineage.json` and semantically re-evaluates the retained phase evidence. It does not accept stored `PASS` labels or hashes alone.

Candidate phase receipts are generated from preregistered command outputs and linked in order under `receipts/lineages/<lineage-id>/`. The writers use exclusive creation and every reference is hash-bound; this is not physical immutability on the local filesystem. A candidate becomes authoritative only when `finalize_lineage.py` creates both its manifest and `receipts/current-lineage.json`.

## Complete v2 release order

Run this only after source and documentation are final. Any selected-file change after `create_lineage.py` invalidates the candidate and requires a new lineage ID.

```powershell
$python = '.\.venv\Scripts\python.exe'
$lineageId = 'v2-2026-08-24' # Choose a new, unique ID.
$lineageDir = "receipts\lineages\$lineageId"
$inputs = "$lineageDir\inputs.json"
$runtimeReceipt = "$lineageDir\phase-01-runtime.json"
$integrationReceipt = "$lineageDir\phase-02-integration.json"
$publicationReceipt = "$lineageDir\phase-03-publication.json"
$featured = 'web\data\featured.json'
$browserEvidence = "$lineageDir\browser-evidence.json"

if (Test-Path -LiteralPath 'receipts\current-lineage.json') {
    throw 'A current lineage already exists; this PoC has no rotation command.'
}

uvx uv@0.12.5 sync --frozen
& $python scripts\create_lineage.py $lineageId

& $python verify\v2_phase.py runtime `
    --inputs $inputs

.\scripts\reproduce.ps1 `
    -Seed 23 `
    -CandidateInputs $inputs

$summary = Get-Content `
    -LiteralPath 'receipts\work\reproduce-latest.json' `
    -Raw |
    ConvertFrom-Json
$productRun = "receipts\work\product-runs\$($summary.run_id)\run.json"

& $python verify\v2_phase.py integration `
    --inputs $inputs `
    --receipt-chain $runtimeReceipt `
    --product-run $productRun

& $python scripts\materialize_web_data.py `
    --run-json $productRun `
    --replace-retained-proof
```

At this point, serve and inspect the web app, then create `$browserEvidence` and its screenshot artifacts under `$lineageDir`. The active verifier validates, but does not generate, this `hash-bound-operator-observation`; the later release audit rehashes its references and checks its exact schema but does not claim to replay the live browser. That manual capture step is a candid release-process gap. The document must bind the candidate lineage and exact featured artifact; cover `/`, `/runs`, `/runs/<run-id>`, `/architecture`, `/contract`, and `/spec`; cover widths 375, 768, 1024, and 1440; and record the enforced console, overflow, keyboard/focus, reduced-motion, origin-security, and four independent status-surface observations.

Once those artifacts exist, continue:

```powershell
& $python verify\v2_phase.py publication `
    --inputs $inputs `
    --receipt-chain $runtimeReceipt `
    --receipt-chain $integrationReceipt `
    --featured $featured `
    --browser-evidence $browserEvidence

$finalizeArgs = @(
    'scripts\finalize_lineage.py',
    '--inputs', $inputs,
    '--phase-receipt', $runtimeReceipt,
    '--phase-receipt', $integrationReceipt,
    '--phase-receipt', $publicationReceipt,
    '--evidence', $featured
)

Get-ChildItem -LiteralPath . -Filter '*.md' -File |
    Sort-Object Name |
    ForEach-Object {
        $finalizeArgs += @('--publication', $_.FullName)
    }

& $python @finalizeArgs

# Rehash and semantically re-evaluate the activated lineage and retained evidence.
& $python verify\release_audit.py

# Prove the ordinary path uses the activated current lineage, not candidate mode.
.\scripts\reproduce.ps1 -Seed 23
```

The three phase commands are deliberately ordered:

- runtime performs exact frozen synchronization, `uv lock --check`, Ruff, strict mypy, compileall, and the bounded hard-exit/runtime verifier;
- integration validates the registered product/query-back contract, loopback-only bindings, Docker health for the collector and SigNoz containers, both HTTP health endpoints, exact exports, and a sanitized service-account identity snapshot copied under the lineage;
- publication performs the frozen web install, production dependency audits (`pnpm audit` and `pip-audit --strict`), the high-severity/high-confidence Bandit scan, package source/README/LICENSE inspection, browser-evidence validation, and publication vocabulary checks.

`web/data/featured.json` must remain byte-identical to the integration-certified product. Phase receipts must be supplied runtime, integration, publication. The finalizer binds every top-level Markdown file and refuses to overwrite an existing manifest or current pointer. There is no atomic pointer rotation or rollback command; that is a production blocker.
