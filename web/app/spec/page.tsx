import type { Metadata } from "next";
import Link from "next/link";
import {
  ArrowRight,
  CircleCheck,
  CircleDashed,
  ShieldAlert,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = { title: "Runtime v2 specification" };

const lifecycle = [
  ["01", "Start activation", "Create one activation and its first attempt."],
  [
    "02",
    "Record intent",
    "Persist the effect boundary before calling the adapter.",
  ],
  [
    "03",
    "Complete once",
    "Commit one business execution and one completion fact.",
  ],
  ["04", "Exit hard", "Terminate the worker with the registered exit code 9."],
  ["05", "Resume fresh", "A new process opens the same durable journal."],
  [
    "06",
    "Resolve",
    "Record an explicit journal resolution, then finish the run.",
  ],
] as const;

const gates = [
  ["Execution", "Two attempts, one intent, one completion, one resolution."],
  ["Projection", "Aware 1 executed + 1 resolved; control 2 executed."],
  ["Query-back", "Every sent span is selected exactly once from local SigNoz."],
  [
    "Release",
    "Frozen inputs and ordered phase receipts validate before activation.",
  ],
] as const;

export default function RuntimeSpecPage() {
  return (
    <div className="mx-auto max-w-[92rem] px-4 py-9 sm:px-6 sm:py-12 lg:px-8">
      <header className="grid gap-6 border-b border-border pb-8 lg:grid-cols-[1fr_0.72fr] lg:items-end">
        <div>
          <Badge tone="brand">Implemented runtime v2</Badge>
          <h1 className="mt-3 max-w-4xl text-balance text-3xl font-semibold tracking-[-0.04em] sm:text-5xl">
            The executable proof, step by step.
          </h1>
        </div>
        <div>
          <p className="text-sm leading-6 text-muted-foreground sm:text-base">
            This page specifies the local runtime sequence. The evidence
            contract defines the admitted document; the architecture page
            separates this implementation from a production target.
          </p>
          <Link
            className="mt-4 inline-flex items-center gap-1.5 text-xs font-medium text-brand outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
            href="/contract"
          >
            Read the evidence contract
            <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        </div>
      </header>

      <section className="border-b border-border py-10">
        <div className="grid gap-6 lg:grid-cols-[0.72fr_1.28fr]">
          <div>
            <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              Registered lifecycle
            </p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight">
              Crash after durable completion.
            </h2>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              The interesting window is deliberate: the effect completed, but
              the first activation did not finish. Resume must use the durable
              fact instead of calling the adapter again.
            </p>
          </div>

          <ol className="overflow-hidden rounded-xl border border-border bg-card">
            {lifecycle.map(([step, title, detail], index) => (
              <li
                className="grid gap-3 border-t border-border p-4 first:border-t-0 sm:grid-cols-[3rem_12rem_1fr] sm:items-center"
                key={step}
              >
                <span className="font-mono text-xs font-semibold text-brand">
                  {step}
                </span>
                <span className="flex items-center gap-2 text-sm font-semibold">
                  {index === 3 ? (
                    <ShieldAlert
                      aria-hidden="true"
                      className="size-4 shrink-0 text-spurious"
                    />
                  ) : (
                    <CircleCheck
                      aria-hidden="true"
                      className="size-4 shrink-0 text-matched"
                    />
                  )}
                  {title}
                </span>
                <span className="text-xs leading-5 text-muted-foreground">
                  {detail}
                </span>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="border-b border-border py-10">
        <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Admission gates
        </p>
        <h2 className="mt-2 text-2xl font-semibold tracking-tight">
          Passing means all four boundaries agree.
        </h2>
        <div className="mt-6 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {gates.map(([title, detail]) => (
            <article
              className="rounded-xl border border-border bg-card p-4"
              key={title}
            >
              <div className="flex items-center gap-2">
                <CircleCheck
                  aria-hidden="true"
                  className="size-4 text-matched"
                />
                <h3 className="text-sm font-semibold">{title}</h3>
              </div>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">
                {detail}
              </p>
            </article>
          ))}
        </div>
      </section>

      <section className="grid gap-5 py-10 lg:grid-cols-2">
        <article className="rounded-xl border border-border bg-card p-5">
          <Badge tone="success">Fail-closed release</Badge>
          <h2 className="mt-3 text-xl font-semibold tracking-tight">
            Evidence cannot silently drift from source.
          </h2>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            Fresh execution remains unavailable until one immutable lineage
            binds frozen inputs, runtime and integration receipts, browser
            observation, publications, and the selected proof document.
          </p>
        </article>
        <article className="rounded-xl border border-dashed border-brand/35 bg-brand/5 p-5">
          <div className="flex items-center gap-2">
            <CircleDashed aria-hidden="true" className="size-4 text-brand" />
            <Badge tone="neutral">Production boundary</Badge>
          </div>
          <h2 className="mt-3 text-xl font-semibold tracking-tight">
            This specification stops at one local host.
          </h2>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            It does not specify multi-worker leases, distributed fencing,
            durable export queues, tenant isolation, disaster recovery, or a
            managed secret boundary. Those remain production design work.
          </p>
        </article>
      </section>
    </div>
  );
}
