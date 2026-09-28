import { ArrowDown, ArrowRight } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type Tone = "brand" | "danger" | "default" | "success";

const toneClasses: Record<Tone, string> = {
  brand: "border-brand/30 bg-brand/10",
  danger: "border-spurious/25 bg-spurious/6",
  default: "border-border bg-card",
  success: "border-matched/25 bg-matched/6",
};

export function SectionHeading({
  eyebrow,
  title,
  accent = false,
}: {
  accent?: boolean;
  eyebrow: string;
  title: string;
}) {
  return (
    <div className="max-w-3xl">
      <p
        className={cn(
          "font-mono text-xs font-semibold uppercase tracking-[0.14em]",
          accent ? "text-brand" : "text-muted-foreground",
        )}
      >
        {eyebrow}
      </p>
      <h2 className="mt-3 text-2xl font-semibold tracking-tight sm:text-3xl">
        {title}
      </h2>
    </div>
  );
}

export function DiagramPlane({
  children,
  label,
}: {
  children: ReactNode;
  label: string;
}) {
  return (
    <div className="rounded-2xl border border-border bg-secondary/40 p-4">
      <p className="mb-4 font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </p>
      {children}
    </div>
  );
}

export function DiagramNode({
  className,
  detail,
  title,
  tone = "default",
}: {
  className?: string;
  detail: string;
  title: string;
  tone?: Tone;
}) {
  return (
    <div
      className={cn(
        "min-w-0 rounded-xl border p-3",
        toneClasses[tone],
        className,
      )}
    >
      <p className="font-mono text-xs font-semibold">{title}</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</p>
    </div>
  );
}

export function DiagramArrow({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex items-center justify-center text-muted-foreground",
        className,
      )}
    >
      <ArrowDown className="size-4 sm:hidden" strokeWidth={1.75} />
      <ArrowRight className="hidden size-4 sm:block" strokeWidth={1.75} />
    </span>
  );
}

export function EdgeLabel({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 rounded-full border border-border bg-secondary/40 px-3 py-2">
      <span className="size-1.5 rounded-full bg-brand" />
      <span className="font-mono text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </span>
    </div>
  );
}

export function TreeBranch({ children }: { children: ReactNode }) {
  return (
    <div className="ml-4 space-y-3 border-l border-border pl-4 sm:ml-7 sm:pl-5">
      {children}
    </div>
  );
}

export function TreeNode({
  detail,
  title,
  tone = "default",
}: {
  detail: string;
  title: string;
  tone?: Tone;
}) {
  const textTone =
    tone === "brand"
      ? "text-brand"
      : tone === "danger"
        ? "text-spurious"
        : tone === "success"
          ? "text-matched"
          : "text-foreground";
  return (
    <div className={cn("rounded-xl border p-3", toneClasses[tone])}>
      <code
        className={cn("break-all font-mono text-xs font-semibold", textTone)}
      >
        {title}
      </code>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</p>
    </div>
  );
}
