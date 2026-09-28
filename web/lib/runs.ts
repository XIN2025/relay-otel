import "server-only";

import { promises as fs } from "node:fs";
import path from "node:path";

import { connection } from "next/server";

import featuredJson from "@/data/featured.json";
import { runDocumentSchema, type RunDocument } from "@/lib/types";

export const featuredRun = runDocumentSchema.parse(featuredJson);

const projectRoot = path.resolve(process.cwd(), "..");
const productRunsRoot = path.join(
  projectRoot,
  "receipts",
  "work",
  "product-runs",
);
const safeRunId = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/;

async function readJson(file: string): Promise<RunDocument | null> {
  try {
    return runDocumentSchema.parse(JSON.parse(await fs.readFile(file, "utf8")));
  } catch {
    return null;
  }
}

export type RunLedgerEntry =
  | { kind: "valid"; run: RunDocument }
  | { id: string; kind: "invalid"; message: string };

export async function runById(id: string): Promise<RunDocument | null> {
  if (id === featuredRun.runId) return featuredRun;
  if (process.env.RELAY_OTEL_EVIDENCE_ONLY === "1") return null;
  // No connection(): /runs/[id] is static and dynamic APIs would throw here.
  if (!safeRunId.test(id)) return null;
  return readJson(path.join(productRunsRoot, id, "run.json"));
}

export async function recentRunLedger(limit = 12): Promise<RunLedgerEntry[]> {
  if (process.env.RELAY_OTEL_EVIDENCE_ONLY === "1") {
    return [{ kind: "valid", run: featuredRun }];
  }
  await connection();
  let directories: string[] = [];
  try {
    directories = (await fs.readdir(productRunsRoot, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory() && safeRunId.test(entry.name))
      .map((entry) => entry.name)
      .sort()
      .reverse();
  } catch {
    return [{ kind: "valid", run: featuredRun }];
  }

  const documents = await Promise.all(
    directories.slice(0, Math.max(0, limit - 1)).map(async (id) => ({
      id,
      run: await readJson(path.join(productRunsRoot, id, "run.json")),
    })),
  );
  return [
    { kind: "valid", run: featuredRun },
    ...documents.map<RunLedgerEntry>(({ id, run }) =>
      run
        ? { kind: "valid", run }
        : {
            id,
            kind: "invalid",
            message:
              "This local evidence document failed structural validation.",
          },
    ),
  ];
}
