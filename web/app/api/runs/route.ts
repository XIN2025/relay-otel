import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { promisify } from "node:util";

import { NextResponse } from "next/server";

import { isTrustedLocalRequest } from "@/lib/local-api";
import {
  minimalPythonEnvironment,
  resolvePython,
  resolveSignozSecretPath,
} from "@/lib/python-runtime";
import { getRuntimeHealth } from "@/lib/runtime-health";
import {
  runDocumentSchema,
  runRequestSchema,
  type RunDocument,
} from "@/lib/types";

export const dynamic = "force-dynamic";
export const maxDuration = 110;
export const runtime = "nodejs";

const executeFile = promisify(execFile);
const productDeadlineSeconds = 90;
const productCallerTimeoutMs = 100_000;
type ActiveRun = { requestId: string; work: Promise<RunDocument> };

let activeRun: ActiveRun | null = null;
const completedRequests = new Map<string, RunDocument>();
const completedRequestLimit = 24;

class RuntimeUnavailableError extends Error {}
class LineageMismatchError extends Error {}
class LineageChangedDuringRunError extends Error {}
class ProductDeadlineError extends Error {}

async function executeFreshRun(seed: number) {
  const projectRoot = path.resolve(process.cwd(), "..");
  const python = await resolvePython(projectRoot);
  try {
    const { stdout } = await executeFile(
      python,
      [
        path.join(projectRoot, "scripts", "product_run.py"),
        "--seed",
        String(seed),
        "--source",
        "web-action",
        "--deadline-seconds",
        String(productDeadlineSeconds),
        "--signoz-api-key-file",
        resolveSignozSecretPath(),
      ],
      {
        cwd: projectRoot,
        env: minimalPythonEnvironment(projectRoot),
        maxBuffer: 2 * 1024 * 1024,
        timeout: productCallerTimeoutMs,
        windowsHide: true,
      },
    );
    const run = runDocumentSchema.parse(JSON.parse(stdout.trim()));
    if (run.schemaVersion !== 2) {
      throw new Error("Fresh runner returned a legacy evidence document.");
    }
    return run;
  } catch (error) {
    if (
      typeof error === "object" &&
      error !== null &&
      "code" in error &&
      (error as { code?: unknown }).code === 78
    ) {
      throw new LineageMismatchError(
        "The current evidence lineage changed or is not activated. Validate and activate a current lineage, then refresh status.",
      );
    }
    if (
      typeof error === "object" &&
      error !== null &&
      "code" in error &&
      (error as { code?: unknown }).code === 75
    ) {
      throw new LineageChangedDuringRunError(
        "The evidence lineage changed after execution began. The runner withheld run.json; inspect the retained local run directory before retrying.",
      );
    }
    if (
      typeof error === "object" &&
      error !== null &&
      "code" in error &&
      (error as { code?: unknown }).code === 74
    ) {
      throw new ProductDeadlineError(
        "The product run exceeded its 90-second end-to-end deadline. Inspect the retained failure artifact before retrying.",
      );
    }
    if (
      typeof error === "object" &&
      error !== null &&
      (("killed" in error && (error as { killed?: unknown }).killed === true) ||
        ("code" in error && (error as { code?: unknown }).code === "ETIMEDOUT"))
    ) {
      throw new ProductDeadlineError(
        "The product process did not exit within the caller grace period after its deadline.",
      );
    }
    throw error;
  }
}

function failureResponse(error: unknown) {
  if (error instanceof RuntimeUnavailableError) {
    return NextResponse.json(
      { code: "runtime_unavailable", error: error.message },
      { headers: { "retry-after": "5" }, status: 503 },
    );
  }
  if (error instanceof LineageMismatchError) {
    return NextResponse.json(
      { code: "lineage_mismatch", error: error.message },
      { status: 409 },
    );
  }
  if (error instanceof LineageChangedDuringRunError) {
    return NextResponse.json(
      { code: "lineage_changed_during_run", error: error.message },
      { status: 409 },
    );
  }
  if (error instanceof ProductDeadlineError) {
    return NextResponse.json(
      { code: "product_deadline_exceeded", error: error.message },
      { status: 504 },
    );
  }
  console.error("Local crash run failed", error);
  return NextResponse.json(
    {
      code: "run_failed",
      error: "The local crash run failed. Check the server log for details.",
    },
    { status: 500 },
  );
}

export async function POST(request: Request) {
  if (!isTrustedLocalRequest(request)) {
    return NextResponse.json(
      { error: "This action is only available from the local app." },
      { status: 403 },
    );
  }
  const mediaType = request.headers
    .get("content-type")
    ?.split(";", 1)[0]
    .trim()
    .toLowerCase();
  if (mediaType !== "application/json") {
    return NextResponse.json(
      { code: "invalid_content_type", error: "Expected application/json." },
      { status: 415 },
    );
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      { error: "Expected a JSON request body." },
      { status: 400 },
    );
  }
  const parsed = runRequestSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      {
        code: "invalid_request",
        error:
          "Use an optional UUID requestId and a seed from 0 through 10000.",
      },
      { status: 400 },
    );
  }

  const requestId = parsed.data.requestId ?? randomUUID();
  const completed = completedRequests.get(requestId);
  if (completed) {
    return NextResponse.json(completed, {
      headers: { "cache-control": "no-store", "x-idempotent-replay": "true" },
      status: 200,
    });
  }
  if (activeRun !== null) {
    if (activeRun.requestId === requestId) {
      try {
        return NextResponse.json(await activeRun.work, {
          headers: {
            "cache-control": "no-store",
            "x-idempotent-replay": "true",
          },
          status: 200,
        });
      } catch (error) {
        return failureResponse(error);
      }
    }
    return NextResponse.json(
      { code: "run_in_progress", error: "A crash run is already in progress." },
      { headers: { "retry-after": "3" }, status: 409 },
    );
  }

  // Reserve the runner before any await.
  const work = (async () => {
    const health = await getRuntimeHealth();
    if (!health.ready) {
      throw new RuntimeUnavailableError(
        "The fresh runner is offline. Review the runtime status before retrying.",
      );
    }
    return executeFreshRun(parsed.data.seed ?? 23);
  })();
  activeRun = { requestId, work };
  try {
    const run = await work;
    completedRequests.set(requestId, run);
    while (completedRequests.size > completedRequestLimit) {
      const oldest = completedRequests.keys().next().value as
        string | undefined;
      if (!oldest) break;
      completedRequests.delete(oldest);
    }
    return NextResponse.json(run, {
      headers: { "cache-control": "no-store" },
      status: 201,
    });
  } catch (error) {
    return failureResponse(error);
  } finally {
    if (activeRun?.work === work) activeRun = null;
  }
}
