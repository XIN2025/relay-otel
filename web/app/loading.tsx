export default function Loading() {
  return (
    <div
      aria-live="polite"
      className="mx-auto min-h-[60vh] max-w-[92rem] px-4 py-10 sm:px-6 lg:px-8"
      role="status"
    >
      <span className="sr-only">Loading evidence workbench</span>
      <div className="h-4 w-36 rounded bg-muted motion-safe:animate-pulse" />
      <div className="mt-4 h-10 max-w-xl rounded bg-muted motion-safe:animate-pulse" />
      <div className="mt-8 grid gap-4 rounded-xl border border-border bg-card p-5 lg:grid-cols-2">
        <div className="h-56 rounded-lg bg-secondary motion-safe:animate-pulse" />
        <div className="h-56 rounded-lg bg-secondary motion-safe:animate-pulse" />
      </div>
    </div>
  );
}
