"use client";

import Link from "next/link";
import { ExternalLink } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import type { ProjectedSpan, RunDocument, TraceArm } from "@/lib/types";
import { cn } from "@/lib/utils";

function spanDepth(span: ProjectedSpan, byId: Map<string, ProjectedSpan>) {
  let depth = 0;
  let parent = span.parent_span_id;
  const visited = new Set<string>();
  while (parent && !visited.has(parent) && depth < 8) {
    visited.add(parent);
    depth += 1;
    parent = byId.get(parent)?.parent_span_id ?? null;
  }
  return depth;
}

function durationLabel(nanoseconds: string) {
  const exact = BigInt(nanoseconds);
  const value = Number(exact);
  if (exact === BigInt(0)) return "0 ns";
  if (exact < BigInt(1_000)) return `${nanoseconds} ns`;
  if (exact < BigInt(1_000_000)) return `${(value / 1_000).toFixed(1)} µs`;
  if (exact < BigInt(1_000_000_000))
    return `${(value / 1_000_000).toFixed(1)} ms`;
  return `${(value / 1_000_000_000).toFixed(2)} s`;
}

function TraceCard({
  arm,
  differenceSpanIds,
  className,
  historical,
}: {
  arm: TraceArm;
  differenceSpanIds: ReadonlySet<string>;
  className?: string;
  historical: boolean;
}) {
  const byId = new Map(arm.spans.map((span) => [span.span_id, span]));
  const executed = arm.spans.filter(
    (span) => span.classification === "executed",
  ).length;
  const resolved = arm.spans.filter(
    (span) =>
      span.classification === "resolved" || span.classification === "replayed",
  ).length;

  return (
    <article
      className={cn(
        "min-w-0 overflow-hidden rounded-xl border border-border bg-card",
        className,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border bg-[#16181d] px-4 py-3.5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold">
              {arm.mode === "aware" ? "Journal-aware" : "Replay-blind control"}
            </h3>
            <Badge tone={arm.mode === "aware" ? "success" : "neutral"}>
              {executed} executed
            </Badge>
            {resolved > 0 && <Badge tone="brand">{resolved} resolved</Badge>}
          </div>
          <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
            {historical && arm.mode === "control"
              ? "retained control service"
              : arm.serviceName}
          </p>
        </div>
        <a
          className="inline-flex items-center gap-1.5 text-xs font-medium text-brand hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          href={arm.signozUrl}
          rel="noreferrer"
          target="_blank"
        >
          Open locally <ExternalLink aria-hidden="true" className="size-3.5" />
        </a>
      </header>

      <ol
        aria-label={`${arm.mode} span tree`}
        className="divide-y divide-border"
      >
        {arm.spans.map((span) => {
          const depth = spanDepth(span, byId);
          const isDecisionRow = differenceSpanIds.has(span.span_id);
          const indentation = ["pl-0", "pl-4", "pl-8", "pl-12"][
            Math.min(depth, 3)
          ];
          return (
            <li
              className={cn(
                "relative px-3 py-2.5 sm:px-4",
                isDecisionRow &&
                  (arm.mode === "aware" ? "bg-brand/7" : "bg-spurious/7"),
              )}
              data-difference={isDecisionRow ? arm.mode : undefined}
              key={span.span_id}
            >
              <div className={cn("min-w-0", indentation)}>
                <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                  <span
                    aria-hidden="true"
                    className="font-mono text-xs text-muted-foreground"
                  >
                    {depth === 0 ? "root" : "└─"}
                  </span>
                  <code className="min-w-0 break-all font-mono text-xs font-medium">
                    {span.name}
                  </code>
                  {(span.classification === "resolved" ||
                    span.classification === "replayed") && (
                    <Badge tone="brand">resolved from journal</Badge>
                  )}
                  {span.status === "ERROR" && (
                    <span className="text-xs font-medium text-spurious">
                      process exited
                    </span>
                  )}
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 font-mono text-xs text-muted-foreground">
                  <span className="break-all">{span.identity}</span>
                  <span>{durationLabel(span.duration_nano)}</span>
                  {span.links.length > 0 && (
                    <span>linked to durable effect</span>
                  )}
                </div>
                {isDecisionRow && (
                  <p
                    className={cn(
                      "mt-1.5 text-xs font-medium",
                      arm.mode === "aware" ? "text-brand" : "text-spurious",
                    )}
                  >
                    {arm.mode === "aware"
                      ? "Journal resolve shown without another execution."
                      : "Journal resolve rendered as a second execution in the control."}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      <footer className="truncate border-t border-border px-4 py-2.5 font-mono text-xs text-muted-foreground">
        trace {arm.traceId}
      </footer>
    </article>
  );
}

export function TraceComparison({ run }: { run: RunDocument }) {
  const [selected, setSelected] = useState<"aware" | "control">("aware");
  const effectKey = (span: ProjectedSpan) => {
    const effectId = span.attributes["relay.effect.id"];
    const attemptId = span.attributes["relay.attempt.id"];
    return typeof effectId === "string" && typeof attemptId === "string"
      ? `${attemptId}:${effectId}`
      : span.identity;
  };
  const differenceKeys = new Set(
    run.aware.spans
      .filter(
        (span) =>
          span.classification === "resolved" ||
          span.classification === "replayed",
      )
      .map(effectKey),
  );
  const differenceSpanIds = new Set(
    [...run.aware.spans, ...run.control.spans]
      .filter((span) => differenceKeys.has(effectKey(span)))
      .map((span) => span.span_id),
  );

  return (
    <section aria-labelledby="trace-comparison-heading">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            Trace explorer
          </p>
          <h2
            className="mt-1.5 text-2xl font-semibold tracking-tight sm:text-3xl"
            id="trace-comparison-heading"
          >
            Same journal, different projection.
          </h2>
          <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted-foreground">
            Compare the resolve row. This workbench verifies the exported tree
            and query-back counts; it does not claim a standard semantic model.
          </p>
        </div>
        <Link
          className="text-xs font-medium text-brand hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          href={`/runs/${encodeURIComponent(run.runId)}`}
        >
          Journal and provenance
        </Link>
      </div>

      <div
        aria-label="Choose trace projection"
        className="mb-3 grid grid-cols-2 rounded-lg border border-border bg-card p-1 lg:hidden"
      >
        <TraceTab
          active={selected === "aware"}
          controls="aware-trace-panel"
          label="Journal-aware"
          onClick={() => setSelected("aware")}
        />
        <TraceTab
          active={selected === "control"}
          controls="control-trace-panel"
          label="Replay-blind control"
          onClick={() => setSelected("control")}
        />
      </div>

      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <div
          className={cn(
            "min-w-0",
            selected === "aware" ? "block" : "hidden lg:block",
          )}
          id="aware-trace-panel"
        >
          <TraceCard
            arm={run.aware}
            differenceSpanIds={differenceSpanIds}
            historical={run.schemaVersion === 1}
          />
        </div>
        <div
          className={cn(
            "min-w-0",
            selected === "control" ? "block" : "hidden lg:block",
          )}
          id="control-trace-panel"
        >
          <TraceCard
            arm={run.control}
            differenceSpanIds={differenceSpanIds}
            historical={run.schemaVersion === 1}
          />
        </div>
      </div>
    </section>
  );
}

function TraceTab({
  active,
  controls,
  label,
  onClick,
}: {
  active: boolean;
  controls: string;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-controls={controls}
      aria-pressed={active}
      className={cn(
        "rounded-md px-2 py-2 text-xs font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active ? "bg-secondary text-foreground" : "text-muted-foreground",
      )}
      onClick={onClick}
      type="button"
    >
      {label}
    </button>
  );
}
