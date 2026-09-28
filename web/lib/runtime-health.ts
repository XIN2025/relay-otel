import "server-only";

import { execFile } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";

import { connection } from "next/server";

import featuredJson from "@/data/featured.json";
import {
  minimalPythonEnvironment,
  resolvePython,
  resolveSignozSecretPath,
} from "@/lib/python-runtime";
import {
  runDocumentSchema,
  runtimeHealthSchema,
  sourceSchemaVersion,
  type RuntimeHealth,
} from "@/lib/types";

const executeFile = promisify(execFile);
const projectRoot = path.resolve(process.cwd(), "..");
const evidenceOnly = process.env.RELAY_OTEL_EVIDENCE_ONLY === "1";
const retainedEvidence = runDocumentSchema.parse(featuredJson);

async function evidenceReadiness(): Promise<RuntimeHealth["evidence"]> {
  const parsed = runDocumentSchema.safeParse(featuredJson);
  if (!parsed.success) {
    return {
      detail: "The retained evidence document failed structural validation.",
      lineageReceipt: null,
      state: "unavailable",
    };
  }
  const version = sourceSchemaVersion(parsed.data);
  return {
    detail:
      version === 1
        ? "Historical evidence loaded from the retained v1 snapshot."
        : "Current-schema retained evidence loaded from the v2 snapshot.",
    lineageReceipt: null,
    state: "retained",
  };
}

async function lineageReadiness(
  python: string,
): Promise<RuntimeHealth["lineage"]> {
  try {
    const { stdout } = await executeFile(
      python,
      ["-m", "relay_otel.lineage", "--project-root", projectRoot],
      {
        cwd: projectRoot,
        env: minimalPythonEnvironment(projectRoot),
        timeout: 5_000,
        windowsHide: true,
      },
    );
    const status: unknown = JSON.parse(stdout.trim());
    if (
      typeof status !== "object" ||
      status === null ||
      (status as Record<string, unknown>).ready !== true ||
      typeof (status as Record<string, unknown>).lineage_id !== "string"
    )
      throw new Error("Invalid lineage status document.");
    return {
      detail: `Current lineage ${(status as Record<string, unknown>).lineage_id} passed registered local lineage validation.`,
      state: "ready",
    };
  } catch {
    return {
      detail: "No validated current evidence lineage is activated.",
      state: "unavailable",
    };
  }
}

async function pythonReadiness(
  python: string,
): Promise<RuntimeHealth["python"]> {
  try {
    await fs.access(path.join(projectRoot, "scripts", "product_run.py"));
    const expectedPython = (
      await fs.readFile(path.join(projectRoot, ".python-version"), "utf8")
    ).trim();
    const probe = [
      "import json, sys",
      "from importlib.metadata import version",
      "expected = {'opentelemetry-api':'1.44.0','opentelemetry-sdk':'1.44.0','opentelemetry-exporter-otlp-proto-http':'1.44.0','PyYAML':'6.0.3','relay-otel':'0.2.0'}",
      "actual = {name: version(name) for name in expected}",
      `assert '.'.join(map(str, sys.version_info[:3])) == ${JSON.stringify(expectedPython)} and actual == expected`,
      "print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])), 'packages': actual}, sort_keys=True))",
    ].join("; ");
    const { stdout } = await executeFile(python, ["-c", probe], {
      env: minimalPythonEnvironment(projectRoot),
      timeout: 5_000,
      windowsHide: true,
    });
    const result: unknown = JSON.parse(stdout.trim());
    if (typeof result !== "object" || result === null)
      throw new Error("Invalid Python environment probe.");
    return {
      detail: `The locked project Python ${expectedPython} environment and exact runtime packages are available.`,
      state: "ready",
    };
  } catch {
    return {
      detail:
        "The project Python interpreter or product runner is unavailable.",
      state: "unavailable",
    };
  }
}

async function collectorReadiness(): Promise<RuntimeHealth["otlp"]> {
  try {
    const response = await fetch("http://127.0.0.1:13133/", {
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(1_500),
    });
    if (response.status !== 200)
      throw new Error("Unhealthy collector response.");
    return {
      detail: "The collector health endpoint responded on loopback port 13133.",
      state: "ready",
    };
  } catch {
    return {
      detail: "The collector health endpoint is unavailable on loopback.",
      state: "unavailable",
    };
  }
}

async function signozReadiness(): Promise<RuntimeHealth["signoz"]> {
  try {
    const response = await fetch("http://127.0.0.1:8080/api/v1/health", {
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(1_500),
    });
    if (!response.ok) throw new Error("Unhealthy response.");
    return {
      detail: "The local SigNoz health endpoint responded successfully.",
      state: "ready",
    };
  } catch {
    return {
      detail: "The local SigNoz health endpoint is unavailable.",
      state: "unavailable",
    };
  }
}

async function readCredential() {
  try {
    const content = await fs.readFile(resolveSignozSecretPath(), "utf8");
    const line = content
      .split(/\r?\n/)
      .find((candidate) => candidate.startsWith("SIGNOZ_API_KEY="));
    return (
      line
        ?.slice("SIGNOZ_API_KEY=".length)
        .trim()
        .replace(/^['"]|['"]$/g, "") || null
    );
  } catch {
    return null;
  }
}

async function credentialReadiness(
  signoz: RuntimeHealth["signoz"],
): Promise<RuntimeHealth["credentials"]> {
  const credential = await readCredential();
  if (!credential) {
    return {
      detail: "The host-local SigNoz API credential is missing.",
      state: "unavailable",
    };
  }
  if (signoz.state !== "ready") {
    return {
      detail:
        "A local credential exists but cannot be validated while SigNoz is offline.",
      state: "unavailable",
    };
  }
  try {
    const response = await fetch(
      "http://127.0.0.1:8080/api/v1/service_accounts/me",
      {
        cache: "no-store",
        headers: { "SIGNOZ-API-KEY": credential },
        redirect: "error",
        signal: AbortSignal.timeout(1_500),
      },
    );
    if (!response.ok) throw new Error("Credential rejected.");
    const document: unknown = await response.json();
    if (typeof document !== "object" || document === null)
      throw new Error("Credential identity response was invalid.");
    const data = (document as Record<string, unknown>).data;
    if (typeof data !== "object" || data === null)
      throw new Error("Credential identity was missing.");
    const identity = data as Record<string, unknown>;
    const roleEntries = identity.serviceAccountRoles;
    if (!Array.isArray(roleEntries))
      throw new Error("Credential roles were missing.");
    const roles = roleEntries.map((entry) => {
      if (typeof entry !== "object" || entry === null)
        throw new Error("Credential role entry was invalid.");
      const role = (entry as Record<string, unknown>).role;
      if (typeof role !== "object" || role === null)
        throw new Error("Credential role was invalid.");
      const name = (role as Record<string, unknown>).name;
      if (typeof name !== "string")
        throw new Error("Credential role name was invalid.");
      return name;
    });
    if (
      identity.name !== "relay-otel-query" ||
      identity.status !== "active" ||
      roles.length !== 1 ||
      roles[0] !== "signoz-viewer"
    )
      throw new Error("Credential has the wrong identity or privilege set.");
    return {
      detail:
        "The local credential identifies the exact active relay-otel-query viewer account.",
      state: "ready",
    };
  } catch {
    return {
      detail: "The local SigNoz API credential could not be validated.",
      state: "unavailable",
    };
  }
}

export async function getRuntimeHealth(): Promise<RuntimeHealth> {
  if (evidenceOnly) {
    return runtimeHealthSchema.parse({
      checkedAt: retainedEvidence.generatedAt,
      credentials: {
        detail:
          "Credentials are intentionally unavailable in the evidence-only export.",
        state: "unavailable",
      },
      evidence: {
        detail:
          "The retained evidence document is embedded in this static export.",
        lineageReceipt: null,
        state: "retained",
      },
      lineage: {
        detail:
          "Current local lineage is not asserted by a hosted evidence snapshot.",
        state: "unavailable",
      },
      otlp: {
        detail: "The local OTLP receiver is outside this evidence-only export.",
        state: "unavailable",
      },
      python: {
        detail:
          "Fresh execution is intentionally disabled in the evidence-only export.",
        state: "unavailable",
      },
      ready: false,
      schemaVersion: 1,
      signoz: {
        detail: "The local SigNoz stack is outside this evidence-only export.",
        state: "unavailable",
      },
    });
  }

  await connection();
  let pythonCommand: string | null = null;
  try {
    pythonCommand = await resolvePython(projectRoot);
  } catch {
    // Each dependent readiness surface reports unavailable below.
  }
  const unavailablePython: RuntimeHealth["python"] = {
    detail: "The locked project Python environment is unavailable.",
    state: "unavailable",
  };
  const unavailableLineage: RuntimeHealth["lineage"] = {
    detail: "Lineage cannot be checked without the locked project Python.",
    state: "unavailable",
  };
  const [evidence, lineage, python, otlp, signoz] = await Promise.all([
    evidenceReadiness(),
    pythonCommand ? lineageReadiness(pythonCommand) : unavailableLineage,
    pythonCommand ? pythonReadiness(pythonCommand) : unavailablePython,
    collectorReadiness(),
    signozReadiness(),
  ]);
  const credentials = await credentialReadiness(signoz);
  return runtimeHealthSchema.parse({
    checkedAt: new Date().toISOString(),
    credentials,
    evidence,
    lineage,
    otlp,
    python,
    ready:
      python.state === "ready" &&
      lineage.state === "ready" &&
      otlp.state === "ready" &&
      signoz.state === "ready" &&
      credentials.state === "ready",
    schemaVersion: 1,
    signoz,
  });
}
