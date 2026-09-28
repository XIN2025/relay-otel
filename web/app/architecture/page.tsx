import type { Metadata } from "next";
import Link from "next/link";
import { ArrowRight, CheckCircle2, CircleDashed } from "lucide-react";

import {
  DiagramArrow,
  DiagramNode,
  DiagramPlane,
  SectionHeading,
} from "@/components/architecture-diagram";
import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = { title: "Architecture" };

const implementedSources = [
  [
    "Durable execution",
    "src/relay/engine.py",
    "Runs graph attempts and resumes from journal state.",
  ],
  [
    "Journal schema",
    "src/relay/journal.py",
    "Records activation, attempt, effect intent, completion, journal resolve, and abandonment.",
  ],
  [
    "Trace projection",
    "src/relay_otel/projector.py",
    "Builds the journal-aware view and the replay-blind control from the same rows.",
  ],
  [
    "OTLP export",
    "src/relay_otel/exporter.py",
    "Sends the completed projection to the configured local OTLP/HTTP endpoint.",
  ],
  [
    "Query-back",
    "src/relay_otel/queryback.py",
    "Requires every exported span to be retrieved before a run is admitted.",
  ],
  [
    "Lineage gate",
    "src/relay_otel/lineage.py",
    "Validates the active evidence lineage before fresh execution can write state.",
  ],
] as const;

export default function ArchitecturePage() {
  return (
    <div className="mx-auto max-w-[92rem] px-4 py-9 sm:px-6 sm:py-12 lg:px-8">
      <header className="grid gap-6 border-b border-border pb-8 lg:grid-cols-[1fr_0.72fr] lg:items-end">
        <div>
          <Badge tone="brand">Implemented and target, separated</Badge>
          <h1 className="mt-3 max-w-4xl text-balance text-3xl font-semibold tracking-[-0.04em] sm:text-5xl">
            Durable truth in. Inspectable projection out.
          </h1>
        </div>
        <div>
          <p className="text-sm leading-6 text-muted-foreground sm:text-base">
            The PoC proves a narrow local boundary. The production design below
            preserves that boundary while replacing synchronous, single-host
            mechanics with durable workers and transport guarantees.
          </p>
          <Link
            className="mt-4 inline-flex items-center gap-1.5 text-xs font-medium text-brand hover:underline"
            href="/#proof"
          >
            Inspect the running boundary{" "}
            <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        </div>
      </header>

      <nav
        aria-label="Architecture sections"
        className="flex flex-wrap gap-2 border-b border-border py-3"
      >
        <ArchitectureLink href="#implemented">Implemented PoC</ArchitectureLink>
        <ArchitectureLink href="#target">Production target</ArchitectureLink>
        <ArchitectureLink href="#source-map">Source ownership</ArchitectureLink>
      </nav>

      <section className="scroll-mt-24 py-10" id="implemented">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <SectionHeading
            eyebrow="01 / implemented locally"
            title="A completed journal is the projection boundary."
          />
          <Badge tone="success">implemented in this PoC</Badge>
        </div>
        <figure
          aria-labelledby="implemented-caption"
          className="mt-6 rounded-xl border border-border bg-card p-4 sm:p-5"
        >
          <figcaption className="sr-only" id="implemented-caption">
            Implemented local execution and post-run projection planes
          </figcaption>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] xl:items-stretch">
            <DiagramPlane label="execution plane / crashable">
              <div className="grid gap-2 sm:grid-cols-[1fr_auto_1fr_auto_1fr] sm:items-center">
                <DiagramNode detail="attempt 1" title="relay worker" />
                <DiagramArrow />
                <DiagramNode
                  detail="WAL + FULL sync"
                  title="SQLite journal"
                  tone="brand"
                />
                <DiagramArrow />
                <DiagramNode
                  detail="registered code 9"
                  title="hard exit"
                  tone="danger"
                />
              </div>
              <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_auto_1fr] sm:items-center">
                <DiagramNode
                  detail="one completed effect"
                  title="effect store"
                  tone="success"
                />
                <DiagramArrow />
                <DiagramNode detail="attempt 2" title="fresh process" />
              </div>
            </DiagramPlane>

            <div className="flex items-center justify-center gap-2 px-1 py-2 xl:flex-col">
              <span className="rounded-full border border-brand/35 bg-brand/10 px-3 py-1 font-mono text-xs font-semibold uppercase tracking-[0.1em] text-brand">
                after run
              </span>
              <DiagramArrow className="text-brand" />
            </div>

            <DiagramPlane label="projection plane / restartable">
              <div className="grid gap-2 sm:grid-cols-[1fr_auto_1fr_auto_1fr] sm:items-center">
                <DiagramNode detail="read completed rows" title="projector" />
                <DiagramArrow />
                <DiagramNode
                  detail="plain local HTTP"
                  title="OTLP receiver"
                  tone="brand"
                />
                <DiagramArrow />
                <DiagramNode detail="trace UI + store" title="SigNoz" />
              </div>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <DiagramNode detail="aware + control" title="two views" />
                <DiagramNode
                  detail="all spans retrieved"
                  title="query-back gate"
                  tone="success"
                />
              </div>
            </DiagramPlane>
          </div>
        </figure>

        <ul className="mt-4 grid gap-2 text-xs leading-5 text-muted-foreground md:grid-cols-3">
          <Limit text="One loopback host and a single in-memory web runner lock." />
          <Limit text="Projection happens after execution; there is no durable export cursor." />
          <Limit text="Plain local OTLP/HTTP and a local credential are acceptable only for this lab." />
        </ul>
      </section>

      <section
        className="scroll-mt-24 border-t border-border py-10"
        id="target"
      >
        <div className="flex flex-wrap items-end justify-between gap-3">
          <SectionHeading
            accent
            eyebrow="02 / production target"
            title="Keep the proof model; replace the delivery mechanics."
          />
          <Badge tone="neutral">design target · not implemented</Badge>
        </div>
        <figure
          aria-labelledby="target-caption"
          className="mt-6 rounded-xl border border-dashed border-brand/35 bg-brand/5 p-4 sm:p-5"
        >
          <figcaption className="sr-only" id="target-caption">
            Recommended production architecture, not implemented in this PoC
          </figcaption>
          <div className="grid gap-2 lg:grid-cols-[repeat(4,minmax(0,1fr)_auto)_minmax(0,1fr)] lg:items-center">
            <TargetNode
              detail="versioned rows + outbox"
              title="durable journal"
            />
            <DiagramArrow />
            <TargetNode
              detail="lease, read cursor, retries"
              title="projector worker"
            />
            <DiagramArrow />
            <TargetNode detail="dead letter + audit trail" title="quarantine" />
            <DiagramArrow />
            <TargetNode
              detail="persistent queue + TLS"
              title="OTel Collector"
            />
            <DiagramArrow />
            <TargetNode detail="ingest + trace query" title="SigNoz" />
          </div>
          <div className="mt-3 grid gap-2 border-t border-brand/20 pt-3 sm:grid-cols-3">
            <TargetNote text="Idempotency key per projected span and export attempt." />
            <TargetNote text="Read-only journal credentials and explicit schema migration policy." />
            <TargetNote text="SLOs for projection lag, rejected spans, retries, and quarantine age." />
          </div>
        </figure>
        <p className="mt-4 max-w-4xl text-sm leading-6 text-muted-foreground">
          The replay-blind control remains a diagnostic comparison, not a normal
          production export stream. Deployment, multi-tenant isolation,
          authentication, retention, disaster recovery, and load behavior are
          outside the implemented PoC.
        </p>
      </section>

      <section
        className="scroll-mt-24 border-t border-border py-10"
        id="source-map"
      >
        <SectionHeading
          eyebrow="source ownership"
          title="Every implemented box has one accountable module."
        />
        <div className="mt-6 overflow-hidden rounded-xl border border-border bg-card">
          {implementedSources.map(([boundary, sourcePath, detail]) => (
            <div
              className="grid gap-2 border-t border-border p-4 first:border-t-0 sm:grid-cols-[11rem_16rem_1fr] sm:items-center"
              key={`${boundary}:${sourcePath}`}
            >
              <span className="text-sm font-medium">{boundary}</span>
              <code className="break-all font-mono text-xs text-brand">
                {sourcePath}
              </code>
              <span className="text-xs leading-5 text-muted-foreground">
                {detail}
              </span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function ArchitectureLink({
  children,
  href,
}: {
  children: React.ReactNode;
  href: string;
}) {
  return (
    <Link
      className="rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground outline-none transition-colors hover:border-brand/40 hover:text-brand focus-visible:ring-2 focus-visible:ring-ring"
      href={href}
    >
      {children}
    </Link>
  );
}

function Limit({ text }: { text: string }) {
  return (
    <li className="flex gap-2 rounded-lg border border-border bg-card p-3">
      <CheckCircle2
        aria-hidden="true"
        className="mt-0.5 size-4 shrink-0 text-muted-foreground"
      />
      {text}
    </li>
  );
}

function TargetNode({ detail, title }: { detail: string; title: string }) {
  return (
    <div className="rounded-lg border border-dashed border-brand/35 bg-background/70 p-3">
      <div className="flex items-center gap-2">
        <CircleDashed aria-hidden="true" className="size-4 text-brand" />
        <p className="font-mono text-xs font-semibold">{title}</p>
      </div>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</p>
    </div>
  );
}

function TargetNote({ text }: { text: string }) {
  return <p className="text-xs leading-5 text-muted-foreground">{text}</p>;
}
