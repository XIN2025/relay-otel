import type { JournalEvent } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function formatPayload(payload: JournalEvent["payload"]) {
  const value = JSON.stringify(payload);
  return value === "{}" ? "" : value;
}

function eventIdentity(event: JournalEvent, key: "activationId" | "attemptId") {
  const value = event[key];
  return typeof value === "string" ? value : null;
}

export function JournalTable({ events }: { events: JournalEvent[] }) {
  const hasAttemptIdentity = events.some(
    (event) =>
      eventIdentity(event, "activationId") || eventIdentity(event, "attemptId"),
  );
  return (
    <div
      aria-label="Durable journal events; scroll horizontally for all columns"
      className="overflow-x-auto rounded-xl border border-border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      role="region"
      tabIndex={0}
    >
      <Table className={hasAttemptIdentity ? "min-w-[62rem]" : "min-w-[44rem]"}>
        <caption className="sr-only">
          Journal events in durable sequence order
        </caption>
        <TableHeader className="bg-secondary/75">
          <TableRow>
            <TableHead className="w-16">Seq</TableHead>
            <TableHead>Durable event</TableHead>
            <TableHead>Node</TableHead>
            {hasAttemptIdentity && <TableHead>Activation / attempt</TableHead>}
            <TableHead>Payload</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {events.map((event) => {
            const activationId = eventIdentity(event, "activationId");
            const attemptId = eventIdentity(event, "attemptId");
            return (
              <TableRow key={event.seq}>
                <TableCell className="tabular-nums text-muted-foreground">
                  {event.seq}
                </TableCell>
                <TableCell>
                  <code className="font-mono text-xs font-medium">
                    {event.type}
                  </code>
                </TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {event.node ?? "workflow"}
                </TableCell>
                {hasAttemptIdentity && (
                  <TableCell className="max-w-56 font-mono text-xs text-muted-foreground">
                    <span
                      className="block truncate"
                      title={activationId ?? undefined}
                    >
                      {activationId ?? "—"}
                    </span>
                    <span
                      className="mt-1 block truncate"
                      title={attemptId ?? undefined}
                    >
                      {attemptId ?? "—"}
                    </span>
                  </TableCell>
                )}
                <TableCell className="max-w-md">
                  <code className="block whitespace-pre-wrap break-all font-mono text-xs leading-5 text-muted-foreground">
                    {formatPayload(event.payload)}
                  </code>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
