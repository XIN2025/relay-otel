import "server-only";

import os from "node:os";
import path from "node:path";

const inheritedEnvironmentKeys = [
  "PATH",
  "SystemRoot",
  "WINDIR",
  "PATHEXT",
  "TEMP",
  "TMP",
] as const;

export async function resolvePython(projectRoot: string) {
  const candidate =
    process.platform === "win32"
      ? path.join(projectRoot, ".venv", "Scripts", "python.exe")
      : path.join(projectRoot, ".venv", "bin", "python");
  const override = process.env.RELAY_PYTHON?.trim();
  if (override) {
    if (!path.isAbsolute(override) || path.normalize(override) !== candidate) {
      throw new Error("RELAY_PYTHON must select this project's locked .venv.");
    }
  }
  return candidate;
}

export function resolveSignozSecretPath() {
  if (process.platform === "win32") {
    const base =
      process.env.LOCALAPPDATA ?? path.join(os.homedir(), "AppData", "Local");
    return path.join(base, "relay-otel-poc", "signoz-root.env");
  }
  const configuredState = process.env.XDG_STATE_HOME;
  const base =
    configuredState && path.isAbsolute(configuredState)
      ? configuredState
      : path.join(os.homedir(), ".local", "state");
  return path.join(base, "relay-otel-poc", "signoz-root.env");
}

export function minimalPythonEnvironment(projectRoot: string) {
  const environment: NodeJS.ProcessEnv = {
    NODE_ENV: process.env.NODE_ENV,
    PYTHONPATH: path.join(projectRoot, "src"),
  };
  for (const key of inheritedEnvironmentKeys) {
    const value = process.env[key];
    if (value !== undefined) environment[key] = value;
  }
  return environment;
}
