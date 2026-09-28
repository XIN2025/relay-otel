"use client";

import { CheckCircle2, Clock3, RefreshCw, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { RuntimeHealth } from "@/lib/types";

type DisplayStatus = {
  detail: string;
  label: string;
  state: "ready" | "retained" | "unavailable";
};

function statuses(health: RuntimeHealth): DisplayStatus[] {
  const runnerReady =
    health.python.state === "ready" && health.lineage.state === "ready";
  const signozReady =
    health.otlp.state === "ready" &&
    health.signoz.state === "ready" &&
    health.credentials.state === "ready";

  return [
    {
      detail: health.evidence.detail,
      label: "Evidence",
      state: health.evidence.state,
    },
    {
      detail: runnerReady
        ? "Python runner and current evidence lineage are validated."
        : health.python.state !== "ready"
          ? health.python.detail
          : health.lineage.detail,
      label: "Runner",
      state: runnerReady ? "ready" : "unavailable",
    },
    {
      detail: signozReady
        ? "UI, OTLP/HTTP, and local API credential are reachable."
        : health.signoz.state !== "ready"
          ? health.signoz.detail
          : health.otlp.state !== "ready"
            ? health.otlp.detail
            : health.credentials.detail,
      label: "SigNoz",
      state: signozReady ? "ready" : "unavailable",
    },
  ];
}

function StateIcon({ state }: Pick<DisplayStatus, "state">) {
  if (state === "ready")
    return <CheckCircle2 aria-hidden="true" className="size-4 text-matched" />;
  if (state === "retained")
    return <Clock3 aria-hidden="true" className="size-4 text-unscored" />;
  return <TriangleAlert aria-hidden="true" className="size-4 text-spurious" />;
}

export function RuntimeStatus({
  health,
  error,
  isRefreshing,
  onRefresh,
  staticEvidence = false,
}: {
  health: RuntimeHealth;
  error: string | null;
  isRefreshing: boolean;
  onRefresh: () => void;
  staticEvidence?: boolean;
}) {
  return (
    <section aria-labelledby="runtime-status-heading">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold" id="runtime-status-heading">
            Runtime preflight
          </h3>
          <p className="mt-0.5 font-mono text-xs text-muted-foreground">
            checked {health.checkedAt.slice(11, 19)} UTC
          </p>
        </div>
        {staticEvidence ? (
          <span className="font-mono text-xs uppercase text-muted-foreground">
            static snapshot
          </span>
        ) : (
          <Button
            aria-label="Refresh runtime status"
            disabled={isRefreshing}
            onClick={onRefresh}
            size="sm"
            type="button"
            variant="ghost"
          >
            <RefreshCw
              aria-hidden="true"
              className={
                isRefreshing ? "size-3.5 motion-safe:animate-spin" : "size-3.5"
              }
            />
            {isRefreshing ? "Checking" : "Refresh"}
          </Button>
        )}
      </div>

      <div
        aria-live="polite"
        className="grid gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-3"
      >
        {statuses(health).map((status) => (
          <div className="min-w-0 bg-card px-3 py-2.5" key={status.label}>
            <div className="flex items-center gap-2">
              <StateIcon state={status.state} />
              <span className="text-xs font-semibold">{status.label}</span>
              <span className="ml-auto font-mono text-xs uppercase text-muted-foreground">
                {status.state === "unavailable" ? "offline" : status.state}
              </span>
            </div>
            <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-muted-foreground">
              {status.detail}
            </p>
          </div>
        ))}
      </div>
      {error && (
        <p className="mt-2 text-xs leading-5 text-spurious" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
