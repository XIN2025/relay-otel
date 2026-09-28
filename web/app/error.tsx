"use client";

import { Button } from "@/components/ui/button";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <div className="mx-auto flex min-h-[60vh] max-w-2xl items-center px-4 py-12 sm:px-6">
      <div
        className="w-full rounded-xl border border-spurious/30 bg-card p-6"
        role="alert"
      >
        <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-spurious">
          View unavailable
        </p>
        <h1 className="mt-2 text-2xl font-semibold">
          The local evidence view could not load.
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          No execution was started. Retry the view; if it still fails, inspect
          the local server log for the bounded error.
        </p>
        <Button className="mt-5" onClick={reset} type="button">
          Retry view
        </Button>
      </div>
    </div>
  );
}
