import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto flex min-h-[60vh] max-w-2xl items-center px-4 py-12 sm:px-6">
      <div className="w-full rounded-xl border border-border bg-card p-6">
        <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Evidence not found
        </p>
        <h1 className="mt-2 text-2xl font-semibold">
          That run is not in the local ledger.
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          The identifier may be invalid, missing, or backed by a rejected
          evidence document.
        </p>
        <Link
          className="mt-5 inline-flex text-sm font-medium text-brand hover:underline"
          href="/runs"
        >
          Return to runs
        </Link>
      </div>
    </div>
  );
}
