import { Activity } from "lucide-react";
import type { Metadata, Viewport } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";

import { SiteNav } from "@/components/site-nav";

import "./globals.css";

const geistSans = Geist({ subsets: ["latin"], variable: "--font-sans" });
const geistMono = Geist_Mono({ subsets: ["latin"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: { default: "relay-otel", template: "%s | relay-otel" },
  description:
    "An unofficial local evidence workbench for crash-safe journal projection.",
};

export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#0a0c10",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      className={`${geistSans.variable} ${geistMono.variable} scroll-smooth motion-reduce:scroll-auto`}
      lang="en"
      style={{ background: "#0a0c10" }}
    >
      <body className="min-h-screen bg-background font-sans text-foreground antialiased selection:bg-brand selection:text-brand-foreground">
        <a
          className="fixed left-4 top-3 z-50 -translate-y-20 rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground shadow-lg transition-transform focus:translate-y-0"
          href="#main-content"
        >
          Skip to content
        </a>
        <header className="sticky top-0 z-40 border-b border-border bg-background/92 backdrop-blur-xl">
          <div className="mx-auto flex min-h-14 max-w-[92rem] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2 sm:flex-nowrap sm:px-6 lg:px-8">
            <Link
              className="flex shrink-0 items-center gap-2 font-mono text-sm font-semibold tracking-tight outline-none focus-visible:ring-2 focus-visible:ring-ring"
              href="/"
            >
              <span className="flex size-7 items-center justify-center rounded-lg border border-spurious/30 bg-spurious/10 text-spurious">
                <Activity
                  aria-hidden="true"
                  className="size-4"
                  strokeWidth={2}
                />
              </span>
              relay-otel
              <span className="hidden rounded border border-border px-1.5 py-0.5 font-sans text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground md:inline">
                unofficial lab
              </span>
            </Link>
            <SiteNav />
            <span className="ml-auto hidden shrink-0 items-center gap-2 text-xs text-muted-foreground sm:flex sm:ml-0">
              <span
                aria-hidden="true"
                className="size-1.5 rounded-full bg-muted-foreground"
              />
              loopback only
            </span>
          </div>
        </header>

        <main id="main-content" tabIndex={-1}>
          {children}
        </main>

        <footer className="border-t border-border">
          <div className="mx-auto flex max-w-[92rem] flex-col gap-2 px-4 py-7 text-xs leading-5 text-muted-foreground sm:flex-row sm:items-center sm:justify-between sm:px-6 lg:px-8">
            <span>Local research artifact · plain OTLP/HTTP output</span>
            <span>
              Not affiliated with or endorsed by SigNoz or OpenTelemetry.
            </span>
          </div>
        </footer>
      </body>
    </html>
  );
}
