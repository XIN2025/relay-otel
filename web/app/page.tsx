import Link from "next/link";
import { ArrowRight, CircleDot, Database, Split } from "lucide-react";

import { DiagramArrow, DiagramNode } from "@/components/architecture-diagram";
import { CrashDemo } from "@/components/crash-demo";
import { Badge } from "@/components/ui/badge";
import { featuredRun } from "@/lib/runs";
import { getRuntimeHealth } from "@/lib/runtime-health";

export default async function HomePage() {
  const health = await getRuntimeHealth();
  const evidenceOnly = process.env.RELAY_OTEL_EVIDENCE_ONLY === "1";

  return (
    <div>
      <section className="mx-auto max-w-[92rem] px-4 pt-7 pb-5 sm:px-6 sm:pt-9 lg:px-8">
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(24rem,0.72fr)] lg:items-end">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="brand">Unofficial local workbench</Badge>
              <span className="font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
                evidence before action
              </span>
            </div>
            <h1 className="mt-3 max-w-4xl text-balance text-3xl font-semibold tracking-[-0.04em] sm:text-4xl lg:text-5xl">
              Prove what survived the crash.
            </h1>
          </div>
          <div>
            <p className="max-w-2xl text-sm leading-6 text-muted-foreground sm:text-base">
              Inspect retained evidence immediately, then run a fresh hard-exit
              and resume only when the local lineage, runner, OTLP receiver, and
              SigNoz are actually ready.
            </p>
            <div className="mt-3 flex flex-wrap gap-x-4 gap-y-2 text-xs">
              <a
                className="font-medium text-brand hover:underline"
                href="#proof"
              >
                Review the proof
              </a>
              <Link
                className="font-medium text-muted-foreground hover:text-foreground"
                href="/contract"
              >
                Read the evidence contract
              </Link>
            </div>
          </div>
        </div>
      </section>

      <CrashDemo
        evidenceOnly={evidenceOnly}
        initialHealth={health}
        initialRun={featuredRun}
      />

      <section className="border-t border-border bg-[#0a0c10]">
        <div className="mx-auto grid max-w-[92rem] gap-6 px-4 py-10 sm:px-6 lg:grid-cols-[0.72fr_1.28fr] lg:items-center lg:px-8">
          <div>
            <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              Boundary, not branding
            </p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
              Durable facts and telemetry views stay separate.
            </h2>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              The implemented PoC projects a completed SQLite journal into two
              local traces. The production target adds a durable projector and
              collector queue; the architecture page keeps those states apart.
            </p>
            <Link
              className="mt-4 inline-flex items-center gap-1.5 text-xs font-medium text-brand hover:underline"
              href="/architecture"
            >
              Implemented versus target{" "}
              <ArrowRight aria-hidden="true" className="size-3.5" />
            </Link>
          </div>

          <figure
            aria-labelledby="home-system-map"
            className="rounded-xl border border-border bg-card p-4"
          >
            <figcaption id="home-system-map" className="sr-only">
              Implemented local path from relay worker to SigNoz
            </figcaption>
            <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
              <DiagramNode
                className="flex-1"
                detail="hard-exit process"
                title="relay worker"
              />
              <DiagramArrow />
              <DiagramNode
                className="flex-1"
                detail="durable facts"
                title="SQLite journal"
                tone="brand"
              />
              <DiagramArrow />
              <DiagramNode
                className="flex-1"
                detail="two local views"
                title="projector"
              />
              <DiagramArrow />
              <DiagramNode
                className="flex-1"
                detail="OTLP + query-back"
                title="local SigNoz"
              />
            </div>
            <div className="mt-3 grid gap-2 border-t border-border pt-3 sm:grid-cols-3">
              <BoundaryFact
                icon={<Database className="size-4" />}
                text="Projection reads the durable SQLite journal"
              />
              <BoundaryFact
                icon={<Split className="size-4" />}
                text="Views are deliberately compared"
              />
              <BoundaryFact
                icon={<CircleDot className="size-4" />}
                text="No endorsement or product audit"
              />
            </div>
          </figure>
        </div>
      </section>
    </div>
  );
}

function BoundaryFact({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <span aria-hidden="true" className="text-brand">
        {icon}
      </span>
      {text}
    </div>
  );
}
