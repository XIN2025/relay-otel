import { spawnSync } from "node:child_process";
import path from "node:path";

const nextBin = path.resolve(
  import.meta.dirname,
  "../node_modules/next/dist/bin/next",
);
const result = spawnSync(process.execPath, [nextBin, "build"], {
  env: { ...process.env, RELAY_OTEL_EVIDENCE_ONLY: "1" },
  stdio: "inherit",
});

if (result.error) throw result.error;
process.exit(result.status ?? 1);
