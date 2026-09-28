"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const routes = [
  ["/", "Proof"],
  ["/runs", "Runs"],
  ["/architecture", "Architecture"],
  ["/contract", "Contract"],
] as const;

export function SiteNav() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Primary"
      className="order-3 flex w-full items-center gap-1 overflow-x-auto sm:order-none sm:w-auto sm:flex-1"
    >
      {routes.map(([href, label]) => {
        const active =
          href === "/" ? pathname === href : pathname.startsWith(href);
        return (
          <Link
            aria-current={active ? "page" : undefined}
            className={
              active
                ? "shrink-0 rounded-lg border border-border bg-secondary px-3 py-1.5 text-xs font-medium text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                : "shrink-0 rounded-lg border border-transparent px-3 py-1.5 text-xs font-medium text-muted-foreground outline-none hover:bg-secondary/60 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            }
            href={href}
            key={href}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
