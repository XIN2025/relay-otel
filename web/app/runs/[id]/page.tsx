import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { JournalTable } from "@/components/journal-table";
import { TraceComparison } from "@/components/trace-tree";
import { Badge } from "@/components/ui/badge";
import { featuredRun, runById } from "@/lib/runs";

export function generateStaticParams() {
  return [{ id: featuredRun.runId }];
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  return { title: (await params).id };
}

export default async function RunPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const run = await runById((await params).id);
  if (!run) notFound();

  return (
    <div className="mx-auto max-w-[92rem] px-4 py-9 sm:px-6 sm:py-12 lg:px-8">
      <header>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="success">{run.status}</Badge>
          <Badge tone={run.schemaVersion === 1 ? "proposed" : "neutral"}>
            {run.schemaVersion === 1 ? "historical v1" : "v2 document"}
          </Badge>
        </div>
        <h1 className="mt-5 break-all font-mono text-2xl font-semibold tracking-tight sm:text-4xl">
          {run.runId}
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
          One hard exit, a fresh process, and one durable journal projected into
          two trace views. The source document passed strict workbench
          invariants before this page rendered. Current claim authority is
          established separately by the runtime lineage validator.
        </p>
      </header>

      <dl className="mt-10 grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-border lg:grid-cols-6">
        <Metric label="hard exit" value={run.hardExitCode} />
        <Metric
          label="activation attempts"
          value={run.groundTruth.activationAttempts}
        />
        <Metric
          label="effect completions"
          value={run.groundTruth.effectCompletions}
        />
        <Metric
          label="provider attempts"
          value={run.groundTruth.providerAttempts ?? "not recorded"}
        />
        <Metric
          label="durable refunds"
          value={run.groundTruth.businessExecutions}
        />
        <Metric
          label="control executed"
          value={run.comparison.controlExecutedEffectSpans}
        />
      </dl>

      <section className="border-b border-border py-14">
        <div className="mb-6 max-w-2xl">
          <p className="font-mono text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
            Ground truth
          </p>
          <h2 className="mt-3 text-2xl font-semibold tracking-tight sm:text-3xl">
            The journal, in durable sequence order
          </h2>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            Rows are shown exactly in durable sequence order. Current v2 runs
            record effect intent, completion, journal resolution, and abandoned
            attempts explicitly; retained v1 evidence is labeled historical and
            is not upgraded into stronger journal claims.
          </p>
        </div>
        <JournalTable events={run.journal.events} />
      </section>

      <section className="py-14">
        <TraceComparison run={run} />
      </section>

      <section className="border-t border-border py-12">
        <h2 className="text-xl font-semibold tracking-tight">Provenance</h2>
        <pre className="mt-5 max-h-80 overflow-auto rounded-xl border border-border bg-secondary/40 p-5 font-mono text-xs leading-5 text-muted-foreground">
          {JSON.stringify(run.provenance, null, 2)}
        </pre>
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="bg-background p-4 sm:p-5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-2 break-words text-2xl font-semibold tabular-nums sm:text-3xl">
        {value}
      </dd>
    </div>
  );
}
