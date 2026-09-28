import { z } from "zod";

const shortStringSchema = z.string().min(1).max(256);
const recordSchema = z
  .record(z.string().max(256), z.unknown())
  .refine((value) => Object.keys(value).length <= 128, "Too many object keys.");
const hexTraceIdSchema = z.string().regex(/^[0-9a-f]{32}$/);
const hexSpanIdSchema = z.string().regex(/^[0-9a-f]{16}$/);
const decimalNanoSchema = z.string().regex(/^\d{1,20}$/);
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const projectRelativePathSchema = z
  .string()
  .min(1)
  .max(512)
  .refine(
    (value) =>
      !value.startsWith("/") &&
      !/^[a-zA-Z]:/.test(value) &&
      !value.split("/").includes("..") &&
      !value.includes("\\"),
    "Expected a confined project-relative POSIX path.",
  );
const legacyNumericNanoSchema = z
  .number()
  .finite()
  .nonnegative()
  .refine(Number.isInteger, "Expected an integer-like legacy timestamp.");
const durationNanoSchema = decimalNanoSchema.refine(
  (value) => BigInt(value) <= BigInt("86400000000000"),
  "Span duration exceeds one day.",
);
const loopbackUrlSchema = z
  .string()
  .url()
  .max(512)
  .refine((value) => {
    const url = new URL(value);
    return (
      url.protocol === "http:" &&
      ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
    );
  }, "Expected a loopback HTTP URL.");

const spanLinkSchema = z
  .object({ span_id: hexSpanIdSchema, trace_id: hexTraceIdSchema })
  .strict();

const commonSpanFields = {
  attributes: recordSchema,
  duration_nano: durationNanoSchema,
  end_time_unix_nano: decimalNanoSchema,
  identity: shortStringSchema,
  kind: z.string().min(1).max(32),
  links: z.array(spanLinkSchema).max(16),
  name: shortStringSchema,
  parent_span_id: hexSpanIdSchema.nullable(),
  span_id: hexSpanIdSchema,
  start_time_unix_nano: decimalNanoSchema,
  status: z.string().min(1).max(32),
  trace_id: hexTraceIdSchema,
};

export const projectedSpanSchema = z
  .object({
    ...commonSpanFields,
    classification: z.enum(["executed", "resolved", "replayed", "structural"]),
  })
  .strict();

const v2ProjectedSpanSchema = projectedSpanSchema.extend({
  classification: z.enum(["executed", "resolved", "structural"]),
});

const legacyProjectedSpanSchema = z
  .object({
    ...commonSpanFields,
    classification: z.enum(["executed", "replayed", "structural"]),
    duration_nano: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    end_time_unix_nano: z.union([decimalNanoSchema, legacyNumericNanoSchema]),
    start_time_unix_nano: z.union([decimalNanoSchema, legacyNumericNanoSchema]),
  })
  .passthrough();

const armFields = {
  serviceName: z.string().min(1).max(128),
  signozUrl: loopbackUrlSchema,
  spans: z.array(projectedSpanSchema).min(1).max(256),
  traceId: hexTraceIdSchema,
};

export const traceArmSchema = z.discriminatedUnion("mode", [
  z.object({ mode: z.literal("aware"), ...armFields }).strict(),
  z.object({ mode: z.literal("control"), ...armFields }).strict(),
]);

const v2ArmFields = {
  serviceName: armFields.serviceName,
  signozUrl: armFields.signozUrl,
  spans: z.array(v2ProjectedSpanSchema).min(1).max(256),
  traceId: armFields.traceId,
};

const legacyArmFields = {
  serviceName: armFields.serviceName,
  signozUrl: armFields.signozUrl,
  spans: z.array(legacyProjectedSpanSchema).min(1).max(256),
  traceId: armFields.traceId,
};

const legacyJournalEventSchema = z
  .object({
    at: z.number().finite(),
    node: z.string().max(128).nullable(),
    payload: recordSchema,
    seq: z.number().int().positive(),
    type: shortStringSchema,
  })
  .passthrough();

const v2JournalEventSchema = z
  .object({
    activationId: z.string().min(1).max(128).nullable(),
    atUnixNano: decimalNanoSchema,
    attemptId: z.string().min(1).max(128).nullable(),
    node: z.string().max(128).nullable(),
    payload: recordSchema,
    seq: z.number().int().positive(),
    type: z.enum([
      "activation_finished",
      "approval_denied",
      "approval_granted",
      "attempt_abandoned",
      "attempt_failed",
      "attempt_started",
      "awaiting_approval",
      "effect_completed",
      "effect_intent",
      "effect_resolved",
      "run_failed",
      "run_finished",
      "run_started",
    ]),
  })
  .strict();

export const journalEventSchema = z.union([
  v2JournalEventSchema,
  legacyJournalEventSchema,
]);

const legacyQueryBackArmSchema = z
  .object({
    queryAttempts: z.number().int().positive().max(100),
    spansRetrieved: z.number().int().positive().max(256),
    spansSent: z.number().int().positive().max(256),
    traceId: hexTraceIdSchema,
    verified: z.literal(true),
  })
  .passthrough();

const retainedQueryRowSchema = z
  .object({
    effectSource: z.enum(["external", "journal"]).nullable(),
    name: shortStringSchema,
    operationName: shortStringSchema,
    parentSpanId: hexSpanIdSchema.nullable(),
    serviceName: z.string().min(1).max(128),
    spanId: hexSpanIdSchema,
    traceId: hexTraceIdSchema,
  })
  .strict();

const retainedQueryRequestSchema = z
  .object({
    compositeQuery: z
      .object({
        queries: z
          .array(
            z
              .object({
                spec: z
                  .object({
                    disabled: z.literal(false),
                    filter: z
                      .object({ expression: z.string().min(1).max(128) })
                      .strict(),
                    limit: z.number().int().positive().max(10_000),
                    name: z.literal("A"),
                    offset: z.literal(0),
                    order: z
                      .array(
                        z
                          .object({
                            direction: z.literal("asc"),
                            key: z
                              .object({ name: z.literal("timestamp") })
                              .strict(),
                          })
                          .strict(),
                      )
                      .length(1),
                    selectFields: z
                      .array(
                        z
                          .object({
                            fieldContext: z.enum(["span", "resource"]),
                            fieldDataType: z.literal("string").optional(),
                            name: z.enum([
                              "trace_id",
                              "span_id",
                              "parent_span_id",
                              "name",
                              "service.name",
                              "relay.operation.name",
                              "relay.effect.source",
                            ]),
                          })
                          .strict(),
                      )
                      .length(7),
                    signal: z.literal("traces"),
                  })
                  .strict(),
                type: z.literal("builder_query"),
              })
              .strict(),
          )
          .length(1),
      })
      .strict(),
    end: z.number().int().positive().max(Number.MAX_SAFE_INTEGER),
    requestType: z.literal("raw"),
    start: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    variables: z.object({}).strict(),
  })
  .strict();

const v2QueryBackArmSchema = z
  .object({
    attributesVerified: z.literal(true),
    duplicateRows: z.literal(0),
    identityVerified: z.literal(true),
    queryAttempts: z.number().int().positive().max(100),
    request: retainedQueryRequestSchema,
    selectedRows: z.array(retainedQueryRowSchema).min(1).max(256),
    serviceNameVerified: z.literal(true),
    spansRetrieved: z.number().int().positive().max(256),
    spansSent: z.number().int().positive().max(256),
    traceId: hexTraceIdSchema,
    verified: z.literal(true),
  })
  .strict();

const sharedRunFields = {
  generatedAt: z.string().datetime({ offset: true }),
  hardExitCode: z.number().int(),
  provenance: recordSchema,
  runId: z.string().min(1).max(128),
  seed: z.number().int().min(0).max(10_000),
  source: z.string().min(1).max(64),
  status: z.literal("finished"),
};

const artifactReferenceSchema = z
  .object({
    bytes: z.number().int().nonnegative(),
    path: projectRelativePathSchema,
    sha256: sha256Schema,
  })
  .strict();

const v2ProvenanceBase = {
  journalPath: projectRelativePathSchema,
  journalSha256: sha256Schema,
  lineageCheckedArtifacts: z.number().int().positive(),
  lineageId: z.string().regex(/^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$/),
  lineageInputsPath: projectRelativePathSchema,
  lineageInputsSha256: sha256Schema,
};

const v2ProvenanceSchema = z.union([
  z
    .object({
      ...v2ProvenanceBase,
      lineageCandidate: z.literal(true),
      lineageManifestPath: z.null(),
      lineageManifestSha256: z.null(),
      phaseReceipts: z
        .record(z.string(), artifactReferenceSchema)
        .refine(
          (value) => Object.keys(value).length === 0,
          "Candidate provenance cannot claim finalized phase receipts.",
        ),
    })
    .strict(),
  z
    .object({
      ...v2ProvenanceBase,
      lineageCandidate: z.undefined().optional(),
      lineageManifestPath: projectRelativePathSchema,
      lineageManifestSha256: sha256Schema,
      phaseReceipts: z
        .record(z.string().min(1).max(64), artifactReferenceSchema)
        .refine(
          (value) => Object.keys(value).length > 0,
          "Current provenance requires phase receipts.",
        ),
    })
    .strict(),
]);

const retainedV1RunSchema = z
  .object({
    ...sharedRunFields,
    aware: z
      .object({ mode: z.literal("aware"), ...legacyArmFields })
      .passthrough(),
    comparison: z
      .object({
        awareExecutedEffectSpans: z.number().int().nonnegative(),
        awareReplayedEffectSpans: z.number().int().nonnegative(),
        naiveExecutedEffectSpans: z.number().int().nonnegative(),
        naiveReplayedEffectSpans: z.number().int().nonnegative(),
      })
      .passthrough(),
    groundTruth: z
      .object({
        businessExecutions: z.number().int().nonnegative(),
        journalEffects: z.number().int().nonnegative(),
        stepAttempts: z.number().int().nonnegative(),
      })
      .passthrough(),
    journal: z
      .object({ events: z.array(legacyJournalEventSchema).min(1).max(512) })
      .passthrough(),
    naive: z
      .object({ mode: z.literal("naive"), ...legacyArmFields })
      .passthrough(),
    queryBack: z
      .object({
        aware: legacyQueryBackArmSchema,
        naive: legacyQueryBackArmSchema,
      })
      .passthrough(),
    schemaVersion: z.literal(1),
    semconvCommit: z.string().regex(/^[0-9a-f]{40}$/),
    semconvStatus: shortStringSchema,
  })
  .passthrough();

const currentV2RunSchema = z
  .object({
    ...sharedRunFields,
    provenance: v2ProvenanceSchema,
    aware: z.object({ mode: z.literal("aware"), ...v2ArmFields }).strict(),
    comparison: z
      .object({
        awareExecutedEffectSpans: z.number().int().nonnegative(),
        awareResolvedEffectSpans: z.number().int().nonnegative(),
        controlExecutedEffectSpans: z.number().int().nonnegative(),
      })
      .strict(),
    control: z.object({ mode: z.literal("control"), ...v2ArmFields }).strict(),
    groundTruth: z
      .object({
        activationAttempts: z.number().int().nonnegative(),
        businessExecutions: z.number().int().nonnegative(),
        effectCompletions: z.number().int().nonnegative(),
        effectIntents: z.number().int().nonnegative(),
        journalResolutions: z.number().int().nonnegative(),
        providerAttempts: z.number().int().nonnegative(),
      })
      .strict(),
    journal: z
      .object({ events: z.array(v2JournalEventSchema).min(1).max(512) })
      .strict(),
    queryBack: z
      .object({ aware: v2QueryBackArmSchema, control: v2QueryBackArmSchema })
      .strict(),
    schemaVersion: z.literal(2),
  })
  .strict();

function normalizeLegacySpan(span: z.infer<typeof legacyProjectedSpanSchema>) {
  return {
    ...span,
    duration_nano: String(span.duration_nano),
    end_time_unix_nano:
      typeof span.end_time_unix_nano === "number"
        ? span.end_time_unix_nano.toFixed(0)
        : span.end_time_unix_nano,
    start_time_unix_nano:
      typeof span.start_time_unix_nano === "number"
        ? span.start_time_unix_nano.toFixed(0)
        : span.start_time_unix_nano,
  };
}

function normalizeLegacyQuery(query: z.infer<typeof legacyQueryBackArmSchema>) {
  return {
    ...query,
    attributesVerified: null,
    duplicateRows: null,
    identityVerified: null,
    serviceNameVerified: null,
  };
}

export const runDocumentSchema = z
  .discriminatedUnion("schemaVersion", [
    retainedV1RunSchema,
    currentV2RunSchema,
  ])
  .transform((run) => {
    if (run.schemaVersion === 2) return run;
    return {
      aware: {
        ...run.aware,
        spans: run.aware.spans.map(normalizeLegacySpan),
      },
      comparison: {
        awareExecutedEffectSpans: run.comparison.awareExecutedEffectSpans,
        awareResolvedEffectSpans: run.comparison.awareReplayedEffectSpans,
        controlExecutedEffectSpans: run.comparison.naiveExecutedEffectSpans,
      },
      control: {
        ...run.naive,
        mode: "control" as const,
        spans: run.naive.spans.map(normalizeLegacySpan),
      },
      generatedAt: run.generatedAt,
      groundTruth: {
        activationAttempts: run.groundTruth.stepAttempts,
        businessExecutions: run.groundTruth.businessExecutions,
        effectCompletions: run.groundTruth.journalEffects,
        effectIntents: null,
        journalResolutions: null,
        providerAttempts: null,
      },
      hardExitCode: run.hardExitCode,
      journal: run.journal,
      provenance: run.provenance,
      queryBack: {
        aware: normalizeLegacyQuery(run.queryBack.aware),
        control: normalizeLegacyQuery(run.queryBack.naive),
      },
      runId: run.runId,
      schemaVersion: 1 as const,
      seed: run.seed,
      source: run.source,
      status: run.status,
    };
  })
  .superRefine((run, context) => {
    const issue = (message: string, path: (string | number)[]) =>
      context.addIssue({ code: "custom", message, path });

    if (run.hardExitCode !== 9)
      issue("The proof requires hard exit code 9.", ["hardExitCode"]);
    if (run.groundTruth.activationAttempts !== 2)
      issue("The proof requires exactly two activation attempts.", [
        "groundTruth",
        "activationAttempts",
      ]);
    if (run.groundTruth.businessExecutions !== 1)
      issue("The proof requires exactly one durable business execution.", [
        "groundTruth",
        "businessExecutions",
      ]);
    if (run.groundTruth.effectCompletions !== 1)
      issue("The proof requires exactly one completed journal effect.", [
        "groundTruth",
        "effectCompletions",
      ]);
    if (run.schemaVersion === 2) {
      if (run.groundTruth.providerAttempts !== 1)
        issue("The v2 proof requires exactly one provider attempt.", [
          "groundTruth",
          "providerAttempts",
        ]);
      if (run.groundTruth.effectIntents !== 1)
        issue("The v2 proof requires exactly one effect intent.", [
          "groundTruth",
          "effectIntents",
        ]);
      if (run.groundTruth.journalResolutions !== 1)
        issue("The v2 proof requires exactly one journal resolution.", [
          "groundTruth",
          "journalResolutions",
        ]);
    }
    if (run.comparison.awareExecutedEffectSpans !== 1)
      issue("The journal-aware view must contain one execution.", [
        "comparison",
        "awareExecutedEffectSpans",
      ]);
    if (run.comparison.awareResolvedEffectSpans !== 1)
      issue("The journal-aware view must contain one journal resolution.", [
        "comparison",
        "awareResolvedEffectSpans",
      ]);
    if (run.comparison.controlExecutedEffectSpans !== 2)
      issue("The replay-blind control must contain two executions.", [
        "comparison",
        "controlExecutedEffectSpans",
      ]);

    if (run.schemaVersion === 2) {
      if (run.aware.spans.length !== 5)
        issue("The registered aware proof requires exactly five spans.", [
          "aware",
          "spans",
        ]);
      if (run.control.spans.length !== 5)
        issue("The registered control proof requires exactly five spans.", [
          "control",
          "spans",
        ]);
    }
    if (run.aware.traceId === run.control.traceId)
      issue("The two projection modes require distinct trace identities.", [
        "control",
        "traceId",
      ]);

    for (let index = 1; index < run.journal.events.length; index += 1) {
      if (run.journal.events[index].seq <= run.journal.events[index - 1].seq)
        issue("Journal sequence numbers must increase strictly.", [
          "journal",
          "events",
          index,
          "seq",
        ]);
    }

    for (const mode of ["aware", "control"] as const) {
      const arm = run[mode];
      const query = run.queryBack[mode];
      const traceUrl = new URL(arm.signozUrl);
      if (traceUrl.pathname !== `/trace/${arm.traceId}`)
        issue("The SigNoz link must target the exported trace identity.", [
          mode,
          "signozUrl",
        ]);
      if (query.traceId !== arm.traceId)
        issue("Query-back trace identity must match the exported arm.", [
          "queryBack",
          mode,
          "traceId",
        ]);
      if (query.spansSent !== arm.spans.length)
        issue("Query-back sent count must match the exported span count.", [
          "queryBack",
          mode,
          "spansSent",
        ]);
      if (query.spansRetrieved !== query.spansSent)
        issue("Query-back must retrieve exactly the exported unique spans.", [
          "queryBack",
          mode,
          "spansRetrieved",
        ]);
      if (run.schemaVersion === 2) {
        const currentQuery = run.queryBack[mode];
        const expectedRows = arm.spans
          .map((span) => ({
            effectSource:
              span.attributes["relay.effect.source"] === "external" ||
              span.attributes["relay.effect.source"] === "journal"
                ? span.attributes["relay.effect.source"]
                : null,
            name: span.name,
            operationName:
              typeof span.attributes["relay.operation.name"] === "string"
                ? span.attributes["relay.operation.name"]
                : "",
            parentSpanId: span.parent_span_id,
            serviceName: arm.serviceName,
            spanId: span.span_id,
            traceId: span.trace_id,
          }))
          .sort((left, right) =>
            [
              left.spanId,
              left.name,
              left.parentSpanId ?? "",
              left.operationName,
              left.effectSource ?? "",
            ]
              .join("|")
              .localeCompare(
                [
                  right.spanId,
                  right.name,
                  right.parentSpanId ?? "",
                  right.operationName,
                  right.effectSource ?? "",
                ].join("|"),
              ),
          );
        if (
          JSON.stringify(currentQuery.selectedRows) !==
          JSON.stringify(expectedRows)
        )
          issue(
            "Retained SigNoz rows must equal the exported selected fields.",
            ["queryBack", mode, "selectedRows"],
          );
        const nanosPerMillisecond = BigInt(1_000_000);
        const queryWindowMillis = BigInt(300_000);
        const starts = arm.spans.map((span) =>
          BigInt(span.start_time_unix_nano),
        );
        const ends = arm.spans.map((span) => BigInt(span.end_time_unix_nano));
        const minStartMillis =
          starts.reduce((left, right) => (left < right ? left : right)) /
          nanosPerMillisecond;
        const queryStart = Number(
          minStartMillis > queryWindowMillis
            ? minStartMillis - queryWindowMillis
            : BigInt(0),
        );
        const queryEnd = Number(
          ends.reduce((left, right) => (left > right ? left : right)) /
            nanosPerMillisecond +
            queryWindowMillis,
        );
        const querySpec = currentQuery.request.compositeQuery.queries[0].spec;
        if (
          currentQuery.request.start !== queryStart ||
          currentQuery.request.end !== queryEnd ||
          querySpec.filter.expression !== `trace_id = '${arm.traceId}'` ||
          querySpec.limit !== Math.max(100, arm.spans.length * 4)
        )
          issue(
            "Retained SigNoz request must equal the registered trace query.",
            ["queryBack", mode, "request"],
          );
      }

      const ids = new Set(arm.spans.map((span) => span.span_id));
      if (ids.size !== arm.spans.length)
        issue("Span IDs must be unique within each arm.", [mode, "spans"]);
      if (arm.spans.filter((span) => span.parent_span_id === null).length !== 1)
        issue("Each arm must have exactly one root span.", [mode, "spans"]);
      for (const [index, span] of arm.spans.entries()) {
        if (span.trace_id !== arm.traceId)
          issue("Every span must belong to its arm trace.", [
            mode,
            "spans",
            index,
            "trace_id",
          ]);
        if (span.parent_span_id && !ids.has(span.parent_span_id))
          issue("Every parent must exist in the same arm.", [
            mode,
            "spans",
            index,
            "parent_span_id",
          ]);
        if (run.schemaVersion === 2) {
          const start = BigInt(span.start_time_unix_nano);
          const end = BigInt(span.end_time_unix_nano);
          if (end < start || end - start !== BigInt(span.duration_nano))
            issue(
              "Span duration must equal its non-negative recorded interval.",
              [mode, "spans", index, "duration_nano"],
            );
          if (span.parent_span_id) {
            const parent = arm.spans.find(
              (candidate) => candidate.span_id === span.parent_span_id,
            );
            if (
              parent &&
              (start < BigInt(parent.start_time_unix_nano) ||
                end > BigInt(parent.end_time_unix_nano))
            )
              issue("Every child interval must stay inside its parent.", [
                mode,
                "spans",
                index,
              ]);
          }
        }
      }

      const root = arm.spans.find((span) => span.parent_span_id === null);
      if (root) {
        const visited = new Set<string>();
        const pending = [root.span_id];
        while (pending.length > 0) {
          const parent = pending.pop();
          if (!parent || visited.has(parent)) continue;
          visited.add(parent);
          for (const span of arm.spans) {
            if (span.parent_span_id === parent) pending.push(span.span_id);
          }
        }
        if (visited.size !== arm.spans.length)
          issue("Every span must be connected to the single trace root.", [
            mode,
            "spans",
          ]);
      }
    }

    const awareExecuted = run.aware.spans.filter(
      (span) => span.classification === "executed",
    );
    const awareResolved = run.aware.spans.filter(
      (span) =>
        span.classification === "resolved" ||
        span.classification === "replayed",
    );
    if (awareExecuted.length !== 1 || awareResolved.length !== 1) {
      issue(
        "The journal-aware arm requires one execution and one resolution.",
        ["aware", "spans"],
      );
    } else {
      const link = awareResolved[0].links;
      if (
        link.length !== 1 ||
        link[0].span_id !== awareExecuted[0].span_id ||
        link[0].trace_id !== run.aware.traceId
      )
        issue("The journal resolution must link to the one executed effect.", [
          "aware",
          "spans",
        ]);
    }

    const controlExecuted = run.control.spans.filter(
      (span) => span.classification === "executed",
    );
    const controlResolved = run.control.spans.filter(
      (span) =>
        span.classification === "resolved" ||
        span.classification === "replayed",
    );
    if (controlExecuted.length !== 2 || controlResolved.length !== 0)
      issue(
        "The replay-blind control requires two executions and no journal-resolution span.",
        ["control", "spans"],
      );

    if (run.schemaVersion === 2) {
      if (run.aware.serviceName !== "relay-otel-aware")
        issue("The aware service name is not the registered v2 resource.", [
          "aware",
          "serviceName",
        ]);
      if (run.control.serviceName !== "relay-otel-control")
        issue("The control service name is not the registered v2 resource.", [
          "control",
          "serviceName",
        ]);

      const journalCounts = new Map<string, number>();
      for (const event of run.journal.events) {
        journalCounts.set(event.type, (journalCounts.get(event.type) ?? 0) + 1);
      }
      const requiredEventCounts: Record<string, number> = {
        activation_finished: 1,
        attempt_abandoned: 1,
        attempt_started: 2,
        effect_completed: 1,
        effect_intent: 1,
        effect_resolved: 1,
        run_finished: 1,
        run_started: 1,
      };
      for (const [type, count] of Object.entries(requiredEventCounts)) {
        if (journalCounts.get(type) !== count)
          issue(`The registered v2 proof requires ${count} ${type} event(s).`, [
            "journal",
            "events",
          ]);
      }
      if (journalCounts.size !== Object.keys(requiredEventCounts).length)
        issue("The registered v2 journal contains an unexpected event type.", [
          "journal",
          "events",
        ]);

      const eventOfType = (type: string) =>
        run.journal.events.find((event) => event.type === type);
      const attempts = run.journal.events.filter(
        (event) => event.type === "attempt_started",
      );
      const intent = eventOfType("effect_intent");
      const completion = eventOfType("effect_completed");
      const resolution = eventOfType("effect_resolved");
      const abandonment = eventOfType("attempt_abandoned");
      const activationFinished = eventOfType("activation_finished");
      const effectIdentityFields = [
        "adapter_version",
        "effect_id",
        "idempotency_key_fingerprint",
        "index",
        "label",
        "request_fingerprint",
        "retry_safe",
      ] as const;
      if (
        attempts.length === 2 &&
        intent &&
        completion &&
        resolution &&
        abandonment &&
        activationFinished
      ) {
        const [firstAttempt, secondAttempt] = attempts;
        if (
          !firstAttempt.activationId ||
          firstAttempt.activationId !== secondAttempt.activationId ||
          !firstAttempt.attemptId ||
          !secondAttempt.attemptId ||
          firstAttempt.attemptId === secondAttempt.attemptId
        )
          issue(
            "The two attempts must be distinct executions of one logical activation.",
            ["journal", "events"],
          );
        if (
          intent.activationId !== firstAttempt.activationId ||
          completion.activationId !== firstAttempt.activationId ||
          resolution.activationId !== firstAttempt.activationId ||
          intent.attemptId !== firstAttempt.attemptId ||
          completion.attemptId !== firstAttempt.attemptId ||
          abandonment.attemptId !== firstAttempt.attemptId ||
          resolution.attemptId !== secondAttempt.attemptId ||
          activationFinished.attemptId !== secondAttempt.attemptId
        )
          issue(
            "Effect and terminal facts do not belong to the registered attempt scopes.",
            ["journal", "events"],
          );
        if (
          completion.payload["source_intent_seq"] !== intent.seq ||
          resolution.payload["source_intent_seq"] !== intent.seq ||
          resolution.payload["source_effect_seq"] !== completion.seq
        )
          issue(
            "Completion and resolution source sequences must form one durable chain.",
            ["journal", "events"],
          );
        if (
          intent.payload["effect_id"] !== "refund" ||
          intent.payload["index"] !== 0 ||
          intent.payload["label"] !== "payments:refund" ||
          intent.payload["adapter_version"] !== "refund-store/v1" ||
          intent.payload["retry_safe"] !== true ||
          typeof intent.payload["request_fingerprint"] !== "string" ||
          !/^[0-9a-f]{64}$/.test(intent.payload["request_fingerprint"]) ||
          typeof intent.payload["idempotency_key_fingerprint"] !== "string" ||
          !/^[0-9a-f]{64}$/.test(intent.payload["idempotency_key_fingerprint"])
        )
          issue(
            "The registered effect identity or retry capability is invalid.",
            ["journal", "events"],
          );
        for (const field of effectIdentityFields) {
          if (
            completion.payload[field] !== intent.payload[field] ||
            resolution.payload[field] !== intent.payload[field]
          )
            issue(
              `Effect identity field ${field} changed across the durable chain.`,
              ["journal", "events"],
            );
        }
      }

      const sensitiveFields = [
        "by",
        "error",
        "input",
        "patch",
        "reason",
        "result",
        "state",
      ];
      for (const [index, event] of run.journal.events.entries()) {
        for (const field of sensitiveFields) {
          if (
            field in event.payload &&
            JSON.stringify(event.payload[field]) !== '{"redacted":true}'
          )
            issue(`Portable v2 journal field ${field} must be redacted.`, [
              "journal",
              "events",
              index,
              "payload",
              field,
            ]);
        }
      }

      for (let index = 1; index < run.journal.events.length; index += 1) {
        if (
          BigInt(run.journal.events[index].atUnixNano) <
          BigInt(run.journal.events[index - 1].atUnixNano)
        )
          issue("V2 journal event times must be non-decreasing.", [
            "journal",
            "events",
            index,
            "atUnixNano",
          ]);
      }

      if (
        awareExecuted[0]?.attributes["relay.operation.name"] !==
          "effect.execute" ||
        awareExecuted[0]?.attributes["relay.effect.source"] !== "external"
      )
        issue(
          "The aware execution must identify an external effect execution.",
          ["aware", "spans"],
        );
      if (
        awareResolved[0]?.attributes["relay.operation.name"] !==
          "effect.resolve" ||
        awareResolved[0]?.attributes["relay.effect.source"] !== "journal"
      )
        issue("The aware resolution must identify a journal-sourced lookup.", [
          "aware",
          "spans",
        ]);
      if (
        controlExecuted.some(
          (span) =>
            span.attributes["relay.operation.name"] !== "effect.execute" ||
            "relay.effect.source" in span.attributes,
        )
      )
        issue(
          "Control executions must remain replay-blind wrapper observations.",
          ["control", "spans"],
        );
    }
  });

export const runRequestSchema = z
  .object({
    requestId: z.string().uuid().optional(),
    seed: z.number().int().min(0).max(10_000).optional(),
  })
  .strict();

export const errorResponseSchema = z
  .object({
    code: z.string().min(1).max(64).optional(),
    error: z.string().min(1).max(512),
  })
  .strict();

const runtimeCheckSchema = z
  .object({
    detail: z.string().min(1).max(512),
    state: z.enum(["ready", "retained", "unavailable"]),
  })
  .strict();

export const runtimeHealthSchema = z
  .object({
    checkedAt: z.string().datetime({ offset: true }),
    credentials: runtimeCheckSchema,
    evidence: runtimeCheckSchema.extend({
      lineageReceipt: z.string().max(128).nullable(),
    }),
    lineage: runtimeCheckSchema,
    otlp: runtimeCheckSchema,
    python: runtimeCheckSchema,
    ready: z.boolean(),
    schemaVersion: z.literal(1),
    signoz: runtimeCheckSchema,
  })
  .strict();

export type JournalEvent = z.infer<typeof journalEventSchema>;
export type ProjectedSpan = z.infer<typeof projectedSpanSchema>;
export type RunDocument = z.infer<typeof runDocumentSchema>;
export type RuntimeHealth = z.infer<typeof runtimeHealthSchema>;
export type TraceArm = z.infer<typeof traceArmSchema>;

export function sourceSchemaVersion(run: RunDocument): 1 | 2 {
  return run.schemaVersion;
}
