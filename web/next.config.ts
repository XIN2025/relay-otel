import path from "node:path";
import type { NextConfig } from "next";

const securityHeaders = [
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      "base-uri 'self'",
      "connect-src 'self'",
      "font-src 'self' data:",
      "form-action 'self'",
      "frame-ancestors 'none'",
      "img-src 'self' data:",
      "object-src 'none'",
      // Next emits inline React bootstrap data. Removing unsafe-inline requires
      // a request nonce and would make these otherwise-static pages dynamic.
      "script-src 'self' 'unsafe-inline'",
      "script-src-attr 'none'",
      "style-src 'self' 'unsafe-inline'",
      "worker-src 'self'",
    ].join("; "),
  },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), geolocation=(), microphone=()",
  },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Content-Type-Options", value: "nosniff" },
];

const evidenceOnly = process.env.RELAY_OTEL_EVIDENCE_ONLY === "1";

const nextConfig: NextConfig = {
  poweredByHeader: false,
  ...(evidenceOnly
    ? {
        output: "export" as const,
        pageExtensions: ["tsx"],
        trailingSlash: true,
      }
    : {
        async headers() {
          return [{ headers: securityHeaders, source: "/(.*)" }];
        },
      }),
  // Pin the Turbopack workspace root to this folder.
  //
  // Without this, Next walks up looking for a lockfile and can find a stray one
  // in a parent directory, making that whole directory the workspace root.
  // `next dev` then recursively watches all of OneDrive/Documents/AppData,
  // which pegs the disk and hangs the machine on "Compiling...".
  turbopack: {
    root: path.join(__dirname),
  },
};

export default nextConfig;
