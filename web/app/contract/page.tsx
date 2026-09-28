import type { Metadata } from "next";

import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = { title: "Evidence contract" };

const facts = [
  ["groundTruth.activationAttempts", "2", "Two durable activation attempts."],
  [
    "groundTruth.effectIntents",
    "1",
    "One intent exists before the effect boundary.",
  ],
  ["groundTruth.effectCompletions", "1", "One effect completion is durable."],
  [
    "groundTruth.journalResolutions",
    "1",
    "The resumed attempt resolves from journal.",
  ],
  [
    "groundTruth.businessExecutions",
    "1",
    "The durable business store records one unique refund row.",
  ],
  [
    "comparison.awareExecutedEffectSpans",
    "1",
    "Journal-aware execution count.",
  ],
  [
    "comparison.awareResolvedEffectSpans",
    "1",
    "One linked journal-resolution span.",
  ],
  ["comparison.controlExecutedEffectSpans", "2", "Replay-blind control count."],
] as const;

export default function ContractPage() {
  return (
    <div className="mx-auto max-w-[92rem] px-4 py-9 sm:px-6 sm:py-12 lg:px-8">
      <header className="max-w-4xl border-b border-border pb-8">
        <Badge tone="brand">Strict v2 · legacy v1 read path</Badge>
        <h1 className="mt-3 text-3xl font-semibold tracking-[-0.035em] sm:text-5xl">
          Evidence contract, without semantic overclaim.
        </h1>
        <p className="mt-4 max-w-3xl text-sm leading-6 text-muted-foreground sm:text-base">
          This contract admits a narrow crash-and-resume proof document. It
          validates durable counts, trace topology, local query-back identity,
          and lineage readiness. It does not claim conformance to an external
          telemetry semantic convention.
        </p>
      </header>

      <section className="grid gap-6 border-b border-border py-10 lg:grid-cols-[0.72fr_1.28fr]">
        <div>
          <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            v2 proof facts
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight">
            Exact claims, exact counters.
          </h2>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            A v2 document is rejected unless all eight values match and the hard
            exit code is 9. Journal sequence numbers must increase strictly.
          </p>
        </div>
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          {facts.map(([field, value, meaning]) => (
            <div
              className="grid gap-2 border-t border-border p-3.5 first:border-t-0 sm:grid-cols-[minmax(15rem,1fr)_3rem_minmax(12rem,1fr)] sm:items-center"
              key={field}
            >
              <code className="break-all font-mono text-xs text-brand">
                {field}
              </code>
              <span className="text-lg font-semibold tabular-nums">
                {value}
              </span>
              <span className="text-xs leading-5 text-muted-foreground">
                {meaning}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="grid gap-5 border-b border-border py-10 lg:grid-cols-3">
        <ContractCard title="Journal rows">
          v2 uses activation and attempt identity plus decimal-string nanosecond
          time. It records effect intent, completion, journal resolution, and
          abandoned attempts explicitly; the UI does not infer resolution from a
          missing row.
        </ContractCard>
        <ContractCard title="Trace arms">
          The journal-aware arm requires one executed effect and one
          resolved-from-journal span linked to that execution. The replay-blind
          control requires two executed spans and no resolution classification.
        </ContractCard>
        <ContractCard title="Query-back">
          Each arm requires verified trace identity and service name, zero
          duplicate rows, a matching trace ID, and retrieved spans exactly equal
          to sent spans.
        </ContractCard>
      </section>

      <section className="grid gap-6 border-b border-border py-10 lg:grid-cols-2">
        <div className="rounded-xl border border-border bg-card p-5">
          <Badge tone="proposed">Retained compatibility only</Badge>
          <h2 className="mt-3 text-xl font-semibold">Historical v1</h2>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            The retained snapshot can still be inspected. Its legacy control arm
            and comparison keys are normalized only in the web view model. Its
            original service identity is preserved in data, and its retired
            convention metadata is neither exposed nor promoted. Fresh runner
            responses must be strict v2.
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card p-5">
          <Badge tone="neutral">Explicit limits</Badge>
          <h2 className="mt-3 text-xl font-semibold">What this cannot prove</h2>
          <ul className="mt-3 space-y-2 text-sm leading-6 text-muted-foreground">
            <li>
              Distributed atomicity or correctness at an uncooperative effect
              boundary.
            </li>
            <li>
              Live instrumentation, production delivery guarantees, or
              multi-tenant safety.
            </li>
            <li>
              SigNoz product quality, endorsement, or external semantic
              conformance.
            </li>
          </ul>
        </div>
      </section>

      <section className="py-10">
        <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Local HTTP boundary
        </p>
        <h2 className="mt-2 text-2xl font-semibold tracking-tight">
          Fail closed before execution.
        </h2>
        <div className="mt-5 grid gap-3 md:grid-cols-2">
          <Endpoint
            method="GET"
            path="/api/runtime"
            text="Reports separate retained evidence, Python, current lineage, OTLP, SigNoz health, and credential-validation states without returning secrets."
          />
          <Endpoint
            method="POST"
            path="/api/runs"
            text="Requires loopback same-origin JSON. A request UUID coalesces duplicates only within this web process; it is not durable job idempotency."
          />
        </div>
        <p className="mt-4 text-xs leading-5 text-muted-foreground">
          Expected failures include 409 for an active run or lineage mismatch,
          503 for an unavailable runtime, and 500 for a bounded runner failure.
          No run is started when preflight is not ready.
        </p>
      </section>
    </div>
  );
}

function ContractCard({
  children,
  title,
}: {
  children: React.ReactNode;
  title: string;
}) {
  return (
    <article className="rounded-xl border border-border bg-card p-5">
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">{children}</p>
    </article>
  );
}

function Endpoint({
  method,
  path,
  text,
}: {
  method: string;
  path: string;
  text: string;
}) {
  return (
    <article className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <Badge tone={method === "GET" ? "success" : "brand"}>{method}</Badge>
        <code className="font-mono text-xs font-semibold">{path}</code>
      </div>
      <p className="mt-3 text-xs leading-5 text-muted-foreground">{text}</p>
    </article>
  );
}
