"use client";

import Link from "next/link";
import {
  ArrowRight,
  Database,
  ExternalLink,
  ShieldCheck,
  Zap,
} from "lucide-react";
import { useState, type ReactNode } from "react";

import { RuntimeStatus } from "@/components/runtime-status";
import { TraceComparison } from "@/components/trace-tree";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  errorResponseSchema,
  runDocumentSchema,
  runtimeHealthSchema,
  type RunDocument,
  type RuntimeHealth,
} from "@/lib/types";

type ActionState = "idle" | "running" | "success" | "error";

function journalHash(run: RunDocument) {
  const value = run.provenance.journalSha256;
  return typeof value === "string" ? value : null;
}

function recoveryMessage(health: RuntimeHealth) {
  if (health.lineage.state !== "ready") {
    return "Fresh execution is locked until a current evidence lineage is validated and activated.";
  }
  if (health.python.state !== "ready") return health.python.detail;
  if (health.signoz.state !== "ready" || health.otlp.state !== "ready") {
    return "Start the local SigNoz stack and its OTLP/HTTP receiver, then refresh status.";
  }
  if (health.credentials.state !== "ready") return health.credentials.detail;
  return "All local dependencies are ready for a fresh crash-and-resume run.";
}

export function CrashDemo({
  evidenceOnly,
  initialHealth,
  initialRun,
}: {
  evidenceOnly: boolean;
  initialHealth: RuntimeHealth;
  initialRun: RunDocument;
}) {
  const [run, setRun] = useState(initialRun);
  const [health, setHealth] = useState(initialHealth);
  const [state, setState] = useState<ActionState>("idle");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [message, setMessage] = useState(
    initialHealth.ready
      ? "Retained evidence is selected. The local runtime is also ready for a fresh run."
      : recoveryMessage(initialHealth),
  );

  async function refreshHealth() {
    setIsRefreshing(true);
    setHealthError(null);
    try {
      const response = await fetch("/api/runtime", { cache: "no-store" });
      const payload: unknown = await response.json();
      const parsed = runtimeHealthSchema.safeParse(payload);
      if (!response.ok || !parsed.success) {
        throw new Error("Runtime preflight returned an invalid response.");
      }
      setHealth(parsed.data);
    } catch (error) {
      setHealthError(
        error instanceof Error
          ? error.message
          : "Runtime status could not be refreshed.",
      );
    } finally {
      setIsRefreshing(false);
    }
  }

  async function crashAndResume() {
    if (!health.ready || state === "running") return;
    const nextRequestId =
      state === "error" && requestId ? requestId : crypto.randomUUID();
    setRequestId(nextRequestId);
    setState("running");
    setMessage(
      "Run accepted. Waiting for the hard exit, fresh-process resume, projection, export, and SigNoz query-back to finish.",
    );
    try {
      const response = await fetch("/api/runs", {
        body: JSON.stringify({
          requestId: nextRequestId,
          seed: Date.now() % 10_001,
        }),
        headers: { "content-type": "application/json" },
        method: "POST",
      });
      const payload: unknown = await response.json();
      if (!response.ok) {
        const failure = errorResponseSchema.safeParse(payload);
        if (
          failure.success &&
          (failure.data.code === "lineage_mismatch" ||
            failure.data.code === "runtime_unavailable")
        ) {
          void refreshHealth();
        }
        throw new Error(
          failure.success
            ? failure.data.error
            : "The local run did not complete.",
        );
      }
      const parsed = runDocumentSchema.safeParse(payload);
      if (!parsed.success) {
        throw new Error("The runner returned an invalid evidence document.");
      }
      setRun(parsed.data);
      setRequestId(null);
      setState("success");
      setMessage(
        `Fresh run ${parsed.data.runId} completed and was queried back from local SigNoz.`,
      );
    } catch (error) {
      setState("error");
      setMessage(
        error instanceof Error
          ? error.message
          : "The local run did not complete.",
      );
      void refreshHealth();
    }
  }

  const isRunning = state === "running";
  const isHistorical = run.schemaVersion === 1 && state !== "success";
  const isFresh = run.schemaVersion === 2 && state === "success";
  const hash = journalHash(run);
  const displayMessage = state === "idle" ? recoveryMessage(health) : message;

  return (
    <section
      className="mx-auto max-w-[92rem] px-4 pb-10 sm:px-6 lg:px-8"
      id="proof"
    >
      <Card className="overflow-hidden rounded-xl shadow-2xl shadow-black/20">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-secondary/45 px-4 py-3 sm:px-5">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <ShieldCheck aria-hidden="true" className="size-4 text-brand" />
            <span className="text-sm font-semibold">Selected proof</span>
            <Badge
              tone={isHistorical ? "proposed" : isFresh ? "success" : "neutral"}
            >
              {isHistorical
                ? "retained v1 evidence"
                : isFresh
                  ? "fresh v2 run"
                  : "retained v2 evidence"}
            </Badge>
          </div>
          <Link
            className="inline-flex items-center gap-1.5 text-xs font-medium text-brand hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            href={`/runs/${encodeURIComponent(run.runId)}`}
          >
            Inspect full run{" "}
            <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        </header>

        <div className="grid min-w-0 lg:grid-cols-[minmax(0,0.92fr)_minmax(30rem,1.08fr)]">
          <div className="min-w-0 border-b border-border p-4 sm:p-5 lg:border-r lg:border-b-0">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                  Durable ground truth
                </p>
                <h2 className="mt-1.5 text-xl font-semibold tracking-tight sm:text-2xl">
                  One effect survived two attempts.
                </h2>
              </div>
              <code
                className="max-w-56 truncate rounded-md border border-border bg-background px-2 py-1 font-mono text-xs text-muted-foreground"
                title={run.runId}
              >
                {run.runId}
              </code>
            </div>

            <dl className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-4">
              <ProofMetric
                label="attempts"
                value={run.groundTruth.activationAttempts}
              />
              <ProofMetric
                label="durable refunds"
                tone="good"
                value={run.groundTruth.businessExecutions}
              />
              <ProofMetric
                label="aware executed"
                tone="good"
                value={run.comparison.awareExecutedEffectSpans}
              />
              <ProofMetric
                label="control executed"
                tone="bad"
                value={run.comparison.controlExecutedEffectSpans}
              />
            </dl>

            <div className="mt-4 grid gap-2 sm:grid-cols-[1fr_auto_1fr_auto_1fr] sm:items-center">
              <ProofStep
                detail={`${run.groundTruth.effectCompletions} durable effect completion`}
                icon={<Database aria-hidden="true" className="size-4" />}
                title="record"
              />
              <FlowArrow />
              <ProofStep
                detail={`process exits ${run.hardExitCode}`}
                icon={
                  <Zap aria-hidden="true" className="size-4 text-spurious" />
                }
                title="crash + resume"
              />
              <FlowArrow />
              <ProofStep
                detail={`${run.queryBack.aware.spansRetrieved + run.queryBack.control.spansRetrieved} spans queried back`}
                icon={<ExternalLink aria-hidden="true" className="size-4" />}
                title="project + verify"
              />
            </div>

            <p className="mt-3 text-xs leading-5 text-muted-foreground">
              The journal-aware view preserves one execution and marks the
              resumed lookup as resolved from journal. The replay-blind control
              renders that journal resolve as a second execution. This is
              projection behavior, not a claim about SigNoz.
            </p>
            {hash && (
              <p
                className="mt-1 truncate font-mono text-xs text-muted-foreground"
                title={hash}
              >
                journal sha256 {hash}
              </p>
            )}
          </div>

          <div aria-busy={isRunning} className="min-w-0 p-4 sm:p-5">
            <RuntimeStatus
              error={healthError}
              health={health}
              isRefreshing={isRefreshing}
              onRefresh={refreshHealth}
              staticEvidence={evidenceOnly}
            />

            <div className="mt-4 rounded-xl border border-border bg-background/65 p-3.5">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className={
                        isRunning
                          ? "size-2 rounded-full bg-brand motion-safe:animate-pulse"
                          : state === "error"
                            ? "size-2 rounded-full bg-spurious"
                            : health.ready
                              ? "size-2 rounded-full bg-matched"
                              : "size-2 rounded-full bg-muted-foreground"
                      }
                    />
                    <h3 className="text-sm font-semibold">Fresh local run</h3>
                  </div>
                  <p
                    aria-live="polite"
                    className={
                      state === "error"
                        ? "mt-1.5 text-xs leading-5 text-spurious"
                        : "mt-1.5 text-xs leading-5 text-muted-foreground"
                    }
                    id="run-status"
                    role={state === "error" ? "alert" : "status"}
                  >
                    {evidenceOnly
                      ? "This hosted review is evidence-only. Run the local workbench to execute a fresh crash and SigNoz query-back."
                      : displayMessage}
                  </p>
                </div>
                <Button
                  aria-describedby="run-status"
                  className="w-full shrink-0 bg-gradient-to-r from-[#4967ff] to-[#7190f9] shadow-lg shadow-blue-950/30 sm:w-auto"
                  data-testid="crash-resume"
                  disabled={
                    evidenceOnly || !health.ready || isRunning || isRefreshing
                  }
                  onClick={crashAndResume}
                  size="lg"
                  type="button"
                >
                  {evidenceOnly
                    ? "Live run disabled in evidence build"
                    : isRunning
                      ? "Run in progress..."
                      : state === "error" && requestId
                        ? "Retry request"
                        : "Crash and resume"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      </Card>

      <div className="pt-10">
        <TraceComparison run={run} />
      </div>
    </section>
  );
}

function ProofMetric({
  label,
  tone,
  value,
}: {
  label: string;
  tone?: "bad" | "good";
  value: number;
}) {
  return (
    <div className="bg-card px-3 py-2.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd
        className={
          tone === "good"
            ? "mt-1 text-2xl font-semibold text-matched tabular-nums"
            : tone === "bad"
              ? "mt-1 text-2xl font-semibold text-spurious tabular-nums"
              : "mt-1 text-2xl font-semibold tabular-nums"
        }
      >
        {value}
      </dd>
    </div>
  );
}

function ProofStep({
  detail,
  icon,
  title,
}: {
  detail: string;
  icon: ReactNode;
  title: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-secondary/30 px-3 py-2">
      <div className="flex items-center gap-2 text-xs font-semibold">
        {icon}
        {title}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{detail}</p>
    </div>
  );
}

function FlowArrow() {
  return (
    <span aria-hidden="true" className="hidden text-muted-foreground sm:block">
      <ArrowRight className="size-4" />
    </span>
  );
}
