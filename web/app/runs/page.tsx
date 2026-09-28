import type { Metadata } from "next";
import Link from "next/link";
import { AlertTriangle, ArrowRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { recentRunLedger } from "@/lib/runs";

export const metadata: Metadata = { title: "Runs" };

export default async function RunsPage() {
  const entries = await recentRunLedger();
  const validCount = entries.filter((entry) => entry.kind === "valid").length;
  const invalidCount = entries.length - validCount;

  return (
    <div className="mx-auto min-h-[70vh] max-w-[92rem] px-4 py-9 sm:px-6 sm:py-12 lg:px-8">
      <header className="flex flex-col gap-5 border-b border-border pb-7 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl">
          <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            Local evidence ledger
          </p>
          <h1 className="mt-2 text-3xl font-semibold tracking-[-0.035em] sm:text-4xl">
            Runs that can be inspected, not inferred.
          </h1>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            Retained v1 evidence is labeled historical. V2 documents are
            admitted only after strict document invariants pass; current claim
            authority remains the separate runtime lineage check. Invalid local
            files stay visible as rejected rows.
          </p>
        </div>
        <dl className="flex gap-6 text-sm">
          <div>
            <dt className="text-xs text-muted-foreground">valid</dt>
            <dd className="mt-1 text-2xl font-semibold tabular-nums">
              {validCount}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">rejected</dt>
            <dd className="mt-1 text-2xl font-semibold tabular-nums">
              {invalidCount}
            </dd>
          </div>
        </dl>
      </header>

      {entries.length === 0 ? (
        <div className="mt-8 rounded-xl border border-dashed border-border p-8 text-center">
          <h2 className="text-base font-semibold">
            No evidence documents found
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            The runner ledger is empty. Return to Proof to review runtime
            status.
          </p>
        </div>
      ) : (
        <section aria-labelledby="ledger-heading" className="mt-7">
          <h2 className="sr-only" id="ledger-heading">
            Evidence run ledger
          </h2>
          <div className="hidden grid-cols-[minmax(18rem,1.4fr)_8rem_repeat(4,minmax(6rem,0.45fr))_1.5rem] gap-4 border-b border-border px-3 py-2 font-mono text-xs uppercase tracking-[0.1em] text-muted-foreground lg:grid">
            <span>Run</span>
            <span>Schema</span>
            <span>Attempts</span>
            <span>Refunds</span>
            <span>Aware</span>
            <span>Control</span>
            <span aria-hidden="true" />
          </div>
          <ul className="divide-y divide-border border-b border-border">
            {entries.map((entry) => {
              if (entry.kind === "invalid") {
                return (
                  <li
                    className="grid gap-2 px-3 py-4 text-sm lg:grid-cols-[minmax(18rem,1.4fr)_1fr] lg:items-center"
                    key={entry.id}
                  >
                    <div className="flex min-w-0 items-center gap-2 text-spurious">
                      <AlertTriangle
                        aria-hidden="true"
                        className="size-4 shrink-0"
                      />
                      <code
                        className="truncate font-mono text-xs"
                        title={entry.id}
                      >
                        {entry.id}
                      </code>
                      <Badge tone="neutral">rejected</Badge>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {entry.message}
                    </p>
                  </li>
                );
              }

              const { run } = entry;
              return (
                <li key={`${run.schemaVersion}:${run.runId}`}>
                  <Link
                    className="group grid gap-4 px-3 py-4 outline-none hover:bg-secondary/35 focus-visible:bg-secondary/45 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring lg:grid-cols-[minmax(18rem,1.4fr)_8rem_repeat(4,minmax(6rem,0.45fr))_1.5rem] lg:items-center"
                    href={`/runs/${encodeURIComponent(run.runId)}`}
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="truncate font-mono text-xs font-semibold group-hover:text-brand">
                          {run.runId}
                        </code>
                        <Badge
                          tone={
                            run.schemaVersion === 1 ? "proposed" : "success"
                          }
                        >
                          {run.schemaVersion === 1
                            ? "historical"
                            : "v2 document"}
                        </Badge>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {new Date(run.generatedAt).toLocaleString("en-IN", {
                          timeZone: "Asia/Kolkata",
                        })}
                      </p>
                    </div>
                    <LedgerDatum
                      label="schema"
                      value={`v${run.schemaVersion}`}
                    />
                    <LedgerDatum
                      label="attempts"
                      value={run.groundTruth.activationAttempts}
                    />
                    <LedgerDatum
                      label="refunds"
                      value={run.groundTruth.businessExecutions}
                    />
                    <LedgerDatum
                      label="aware"
                      value={run.comparison.awareExecutedEffectSpans}
                    />
                    <LedgerDatum
                      label="control"
                      value={run.comparison.controlExecutedEffectSpans}
                    />
                    <ArrowRight
                      aria-hidden="true"
                      className="hidden size-4 text-muted-foreground group-hover:text-brand lg:block"
                    />
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}

function LedgerDatum({
  label,
  value,
}: {
  label: string;
  value: number | string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 lg:block">
      <span className="text-xs text-muted-foreground lg:sr-only">{label}</span>
      <span className="text-sm font-semibold tabular-nums">{value}</span>
    </div>
  );
}
