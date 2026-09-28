from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar, cast

from .graph import Graph, NodeContext, State
from .journal import JOURNAL_SCHEMA_VERSION, Event, EventType, Journal

EffectResultT = TypeVar("EffectResultT")
RunStatus = Literal["running", "awaiting_approval", "finished", "failed"]


class Halt(Exception):
    """Leave the current attempt open, simulating abrupt process loss."""


class JournalLifecycleError(ValueError):
    """The event sequence is not a valid runtime-v2 lifecycle."""


class EffectIdentityMismatch(RuntimeError):
    """A resumed call does not describe the effect recorded for this activation."""


class UnknownEffectOutcome(RuntimeError):
    """An intent has no completion and is unsafe to invoke generically again."""


@dataclass(frozen=True)
class RunState:
    state: State
    next_node: str
    status: RunStatus
    completed: list[str]
    activation_id: str | None
    open_attempt_id: str | None
    approval_granted: bool
    failure_reason: str | None = None


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise JournalLifecycleError(f"journal field {field!r} must be non-empty text")
    return value


def _same_scope(
    event: Event, *, node: str, activation_id: str, attempt_id: str | None = None
) -> None:
    if event.node != node or event.activation_id != activation_id:
        raise JournalLifecycleError(
            f"event {event.seq} does not match active node/activation"
        )
    if attempt_id is not None and event.attempt_id != attempt_id:
        raise JournalLifecycleError(f"event {event.seq} does not match active attempt")


EFFECT_IDENTITY_FIELDS = (
    "effect_id",
    "index",
    "label",
    "request_fingerprint",
    "adapter_version",
    "idempotency_key_fingerprint",
    "retry_safe",
)


def _effect_identity(event: Event) -> dict[str, object]:
    identity = {key: event.payload.get(key) for key in EFFECT_IDENTITY_FIELDS}
    _required_text(identity["effect_id"], "effect_id")
    index = identity["index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise JournalLifecycleError(
            f"event {event.seq} effect index must be a non-negative integer"
        )
    _required_text(identity["label"], "label")
    _required_text(identity["request_fingerprint"], "request_fingerprint")
    adapter_version = _required_text(identity["adapter_version"], "adapter_version")
    if adapter_version == "unspecified":
        raise JournalLifecycleError(
            f"event {event.seq} effect adapter version is not explicit"
        )
    fingerprint = identity["idempotency_key_fingerprint"]
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or not fingerprint
    ):
        raise JournalLifecycleError(
            f"event {event.seq} idempotency fingerprint is invalid"
        )
    retry_safe = identity["retry_safe"]
    if not isinstance(retry_safe, bool):
        raise JournalLifecycleError(f"event {event.seq} retry_safe must be boolean")
    if retry_safe and fingerprint is None:
        raise JournalLifecycleError(
            f"event {event.seq} retry-safe effect lacks an idempotency fingerprint"
        )
    return identity


def _effect_time(event: Event, field: str) -> int:
    value = event.payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JournalLifecycleError(
            f"event {event.seq} field {field!r} must be a positive integer"
        )
    return value


def _same_effect(source: Event, event: Event) -> None:
    if (source.node, source.activation_id) != (event.node, event.activation_id):
        raise JournalLifecycleError(
            f"effect event {event.seq} changes source event {source.seq} scope"
        )
    if _effect_identity(source) != _effect_identity(event):
        raise JournalLifecycleError(
            f"effect event {event.seq} changes source event {source.seq} identity"
        )


def fold(graph: Graph, events: list[Event]) -> RunState:

    if not events:
        raise JournalLifecycleError("run has no run_started event")
    run_id = events[0].run_id
    if any(event.run_id != run_id for event in events):
        raise JournalLifecycleError("fold received events from multiple runs")
    first = events[0]
    if first.type is not EventType.RUN_STARTED:
        raise JournalLifecycleError("first event must be run_started")
    if first.payload.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION:
        raise JournalLifecycleError(
            "legacy journal cannot be resumed or replayed by runtime v2"
        )
    workflow_version = _required_text(
        first.payload.get("workflow_version"), "workflow_version"
    )
    if workflow_version != graph.version:
        raise JournalLifecycleError(
            f"workflow version mismatch: journal={workflow_version!r}, "
            f"runtime={graph.version!r}"
        )
    value = first.payload.get("input")
    if not isinstance(value, dict):
        raise JournalLifecycleError("journal input must be an object")
    state = cast(State, dict(value))
    current_node = _required_text(first.payload.get("start_node"), "start_node")
    activation_id = _required_text(first.payload.get("activation_id"), "activation_id")
    if first.node != current_node or first.activation_id != activation_id:
        raise JournalLifecycleError(
            "run_started activation columns disagree with payload"
        )
    if current_node != graph.START or current_node not in graph.nodes:
        raise JournalLifecycleError(
            "run_started does not target this graph's START node"
        )

    status: RunStatus = "running"
    failure_reason: str | None = None
    open_attempt_id: str | None = None
    approval_granted = False
    completed: list[str] = []
    activation_ids = {activation_id}
    effect_intents: dict[int, Event] = {}
    effect_completions: dict[int, Event] = {}
    completed_intents: set[int] = set()
    terminal = False

    for event in events[1:]:
        if terminal:
            raise JournalLifecycleError(f"event {event.seq} follows a terminal event")

        if event.type is EventType.ATTEMPT_STARTED:
            if status != "running" or open_attempt_id is not None:
                raise JournalLifecycleError(
                    f"attempt_started {event.seq} is not valid in the current state"
                )
            _same_scope(event, node=current_node, activation_id=activation_id)
            if graph.nodes[current_node].requires_approval and not approval_granted:
                raise JournalLifecycleError(
                    f"attempt_started {event.seq} bypasses required approval"
                )
            open_attempt_id = _required_text(event.attempt_id, "attempt_id")

        elif event.type in (
            EventType.EFFECT_INTENT,
            EventType.EFFECT_COMPLETED,
            EventType.EFFECT_RESOLVED,
        ):
            if status != "running" or open_attempt_id is None:
                raise JournalLifecycleError(
                    f"effect event {event.seq} has no open attempt"
                )
            _same_scope(
                event,
                node=current_node,
                activation_id=activation_id,
                attempt_id=open_attempt_id,
            )
            _effect_identity(event)
            if event.type is EventType.EFFECT_INTENT:
                _effect_time(event, "started_at_ns")
                retry_of = event.payload.get("retry_of_intent_seq")
                if retry_of is not None:
                    if isinstance(retry_of, bool) or not isinstance(retry_of, int):
                        raise JournalLifecycleError(
                            f"effect intent {event.seq} has invalid retry source"
                        )
                    source = effect_intents.get(retry_of)
                    if source is None or retry_of in completed_intents:
                        raise JournalLifecycleError(
                            f"effect intent {event.seq} retries no pending source"
                        )
                    _same_effect(source, event)
                    if event.payload.get("retry_safe") is not True:
                        raise JournalLifecycleError(
                            f"effect intent {event.seq} retries an unsafe operation"
                        )
                effect_intents[event.seq] = event
            elif event.type is EventType.EFFECT_COMPLETED:
                source_seq = event.payload.get("source_intent_seq")
                if isinstance(source_seq, bool) or not isinstance(source_seq, int):
                    raise JournalLifecycleError(
                        f"effect completion {event.seq} has invalid source intent"
                    )
                source = effect_intents.get(source_seq)
                if source is None or source_seq in completed_intents:
                    raise JournalLifecycleError(
                        f"effect completion {event.seq} has no unique source intent"
                    )
                _same_effect(source, event)
                if source.attempt_id != event.attempt_id:
                    raise JournalLifecycleError(
                        f"effect completion {event.seq} changes source attempt"
                    )
                started_at_ns = _effect_time(event, "started_at_ns")
                ended_at_ns = _effect_time(event, "ended_at_ns")
                if ended_at_ns < started_at_ns:
                    raise JournalLifecycleError(
                        f"effect completion {event.seq} has negative duration"
                    )
                completed_intents.add(source_seq)
                effect_completions[event.seq] = event
            else:
                source_seq = event.payload.get("source_effect_seq")
                if isinstance(source_seq, bool) or not isinstance(source_seq, int):
                    raise JournalLifecycleError(
                        f"effect resolution {event.seq} has invalid source completion"
                    )
                source = effect_completions.get(source_seq)
                if source is None:
                    raise JournalLifecycleError(
                        f"effect resolution {event.seq} has no source completion"
                    )
                _same_effect(source, event)
                source_intent_seq = event.payload.get("source_intent_seq")
                if source.payload.get("source_intent_seq") != source_intent_seq:
                    raise JournalLifecycleError(
                        f"effect resolution {event.seq} names the wrong source intent"
                    )
                started_at_ns = _effect_time(event, "started_at_ns")
                ended_at_ns = _effect_time(event, "ended_at_ns")
                if ended_at_ns < started_at_ns:
                    raise JournalLifecycleError(
                        f"effect resolution {event.seq} has negative duration"
                    )

        elif event.type is EventType.ATTEMPT_ABANDONED:
            if status != "running" or open_attempt_id is None:
                raise JournalLifecycleError(
                    f"attempt_abandoned {event.seq} has no open attempt"
                )
            _same_scope(
                event,
                node=current_node,
                activation_id=activation_id,
                attempt_id=open_attempt_id,
            )
            open_attempt_id = None

        elif event.type is EventType.ATTEMPT_FAILED:
            if status != "running" or open_attempt_id is None:
                raise JournalLifecycleError(
                    f"attempt_failed {event.seq} has no open attempt"
                )
            _same_scope(
                event,
                node=current_node,
                activation_id=activation_id,
                attempt_id=open_attempt_id,
            )
            failure_reason = str(event.payload.get("error", "attempt failed"))
            open_attempt_id = None
            status = "failed"

        elif event.type is EventType.ACTIVATION_FINISHED:
            if status != "running" or open_attempt_id is None:
                raise JournalLifecycleError(
                    f"activation_finished {event.seq} has no open attempt"
                )
            _same_scope(
                event,
                node=current_node,
                activation_id=activation_id,
                attempt_id=open_attempt_id,
            )
            patch = event.payload.get("patch")
            if not isinstance(patch, dict):
                raise JournalLifecycleError("activation patch must be an object")
            state.update(patch)
            completed.append(current_node)
            open_attempt_id = None
            next_node = _required_text(event.payload.get("next_node"), "next_node")
            if next_node == Graph.END:
                current_node = Graph.END
                activation_id = ""
                approval_granted = False
            else:
                if next_node not in graph.nodes:
                    raise JournalLifecycleError(
                        f"recorded route targets undefined node {next_node!r}"
                    )
                next_activation_id = _required_text(
                    event.payload.get("next_activation_id"), "next_activation_id"
                )
                if next_activation_id in activation_ids:
                    raise JournalLifecycleError(
                        "activation ids must be unique within a run"
                    )
                activation_ids.add(next_activation_id)
                current_node = next_node
                activation_id = next_activation_id
                approval_granted = False

        elif event.type is EventType.AWAITING_APPROVAL:
            if (
                status != "running"
                or open_attempt_id is not None
                or approval_granted
                or not graph.nodes[current_node].requires_approval
            ):
                raise JournalLifecycleError(
                    f"awaiting_approval {event.seq} is not valid in the current state"
                )
            _same_scope(event, node=current_node, activation_id=activation_id)
            status = "awaiting_approval"
            approval_granted = False

        elif event.type is EventType.APPROVAL_GRANTED:
            if status != "awaiting_approval":
                raise JournalLifecycleError(
                    f"approval_granted {event.seq} has no pending approval"
                )
            _same_scope(event, node=current_node, activation_id=activation_id)
            status = "running"
            approval_granted = True

        elif event.type is EventType.APPROVAL_DENIED:
            if status != "awaiting_approval":
                raise JournalLifecycleError(
                    f"approval_denied {event.seq} has no pending approval"
                )
            _same_scope(event, node=current_node, activation_id=activation_id)
            status = "failed"
            failure_reason = "approval denied"

        elif event.type is EventType.RUN_FINISHED:
            if status != "running" or open_attempt_id is not None:
                raise JournalLifecycleError(
                    f"run_finished {event.seq} is not valid in the current state"
                )
            if current_node != Graph.END:
                raise JournalLifecycleError(
                    "run_finished before the recorded END route"
                )
            status = "finished"
            terminal = True

        elif event.type is EventType.RUN_FAILED:
            if status != "failed" or open_attempt_id is not None:
                raise JournalLifecycleError(
                    f"run_failed {event.seq} lacks a preceding durable failure fact"
                )
            failure_reason = str(
                event.payload.get("reason", failure_reason or "run failed")
            )
            terminal = True

        else:
            raise JournalLifecycleError(
                f"legacy or unsupported event {event.type.value!r} at sequence {event.seq}"
            )

    return RunState(
        state=state,
        next_node=current_node,
        status=status,
        completed=completed,
        activation_id=activation_id or None,
        open_attempt_id=open_attempt_id,
        approval_granted=approval_granted,
        failure_reason=failure_reason,
    )


def _canonical_fingerprint(request: object) -> str:
    try:
        canonical = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TypeError("effect request metadata must be canonical JSON") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Engine:
    def __init__(self, graph: Graph, journal: Journal) -> None:
        if not graph.version or graph.version == "unversioned":
            raise ValueError("runtime v2 requires an explicit workflow version")
        problems = graph.validate()
        if problems:
            raise ValueError("invalid graph: " + "; ".join(problems))
        self.graph = graph
        self.journal = journal

    def _context(
        self,
        run_id: str,
        node: str,
        activation_id: str,
        attempt_id: str,
        events: list[Event],
    ) -> NodeContext:
        intents = {
            event.seq: event
            for event in events
            if event.type is EventType.EFFECT_INTENT
            and event.activation_id == activation_id
        }
        completed_by_intent = {
            int(event.payload["source_intent_seq"]): event
            for event in events
            if event.type is EventType.EFFECT_COMPLETED
            and event.activation_id == activation_id
        }
        completed_by_id: dict[str, Event] = {}
        completed_by_index: dict[int, Event] = {}
        for event in completed_by_intent.values():
            effect_id = _required_text(event.payload.get("effect_id"), "effect_id")
            index = int(event.payload.get("index", -1))
            if effect_id in completed_by_id or index in completed_by_index:
                raise JournalLifecycleError(
                    "one activation contains duplicate completed effect identities"
                )
            completed_by_id[effect_id] = event
            completed_by_index[index] = event
        pending_by_id: dict[str, Event] = {}
        pending_by_index: dict[int, Event] = {}
        for seq, event in intents.items():
            if seq in completed_by_intent:
                continue
            effect_id = _required_text(event.payload.get("effect_id"), "effect_id")
            index = int(event.payload.get("index", -1))
            pending_by_id[effect_id] = event
            pending_by_index[index] = event

        call_index = 0

        def call(
            label: str,
            fn: Callable[[], EffectResultT],
            *,
            request: object = None,
            adapter_version: str = "unspecified",
            effect_id: str | None = None,
            idempotency_key: str | None = None,
            retry_safe: bool = False,
        ) -> EffectResultT:
            nonlocal call_index
            index = call_index
            call_index += 1
            stable_effect_id = effect_id or f"call-{index}"
            if (
                not label
                or not adapter_version
                or adapter_version == "unspecified"
                or not stable_effect_id
            ):
                raise ValueError(
                    "effect label/id and an explicit adapter version are required"
                )
            if retry_safe and not idempotency_key:
                raise ValueError("retry-safe effects require a stable idempotency key")
            request_fingerprint = _canonical_fingerprint(request)
            idempotency_fingerprint = (
                hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
                if idempotency_key
                else None
            )
            identity: dict[str, object] = {
                "effect_id": stable_effect_id,
                "index": index,
                "label": label,
                "request_fingerprint": request_fingerprint,
                "adapter_version": adapter_version,
                "idempotency_key_fingerprint": idempotency_fingerprint,
                "retry_safe": retry_safe,
            }

            recorded = completed_by_id.get(stable_effect_id)
            index_recorded = completed_by_index.get(index)
            pending = pending_by_id.get(stable_effect_id)
            index_pending = pending_by_index.get(index)
            collision = recorded or pending
            if collision is None:
                collision = index_recorded or index_pending
            if collision is not None:
                mismatches = [
                    key
                    for key, expected in identity.items()
                    if collision.payload.get(key) != expected
                ]
                if mismatches:
                    raise EffectIdentityMismatch(
                        f"effect identity mismatch for activation {activation_id}: "
                        + ", ".join(sorted(mismatches))
                    )

            if recorded is not None:
                started_at_ns = time.time_ns()
                result = recorded.payload.get("result")
                ended_at_ns = time.time_ns()
                self.journal.append(
                    run_id,
                    EventType.EFFECT_RESOLVED,
                    node,
                    {
                        **identity,
                        "source_effect_seq": recorded.seq,
                        "source_intent_seq": recorded.payload["source_intent_seq"],
                        "started_at_ns": started_at_ns,
                        "ended_at_ns": ended_at_ns,
                    },
                    activation_id=activation_id,
                    attempt_id=attempt_id,
                )
                return cast(EffectResultT, result)

            retry_of_intent_seq: int | None = None
            if pending is not None:
                if not retry_safe or not idempotency_key:
                    raise UnknownEffectOutcome(
                        f"effect {stable_effect_id!r} has intent sequence {pending.seq} "
                        "without completion; outcome is unknown and generic retry is unsafe"
                    )
                retry_of_intent_seq = pending.seq

            started_at_ns = time.time_ns()
            intent = self.journal.append(
                run_id,
                EventType.EFFECT_INTENT,
                node,
                {
                    **identity,
                    "started_at_ns": started_at_ns,
                    "retry_of_intent_seq": retry_of_intent_seq,
                },
                activation_id=activation_id,
                attempt_id=attempt_id,
                at_ns=started_at_ns,
            )
            intents[intent.seq] = intent
            pending_by_id[stable_effect_id] = intent
            pending_by_index[index] = intent
            result = fn()
            ended_at_ns = time.time_ns()
            completion = self.journal.append(
                run_id,
                EventType.EFFECT_COMPLETED,
                node,
                {
                    **identity,
                    "source_intent_seq": intent.seq,
                    "started_at_ns": started_at_ns,
                    "ended_at_ns": ended_at_ns,
                    "result": result,
                },
                activation_id=activation_id,
                attempt_id=attempt_id,
                at_ns=ended_at_ns,
            )
            completed_by_intent[intent.seq] = completion
            completed_by_id[stable_effect_id] = completion
            completed_by_index[index] = completion
            pending_by_id.pop(stable_effect_id, None)
            pending_by_index.pop(index, None)
            return result

        return NodeContext(
            run_id=run_id,
            node=node,
            activation_id=activation_id,
            attempt_id=attempt_id,
            call=call,
        )

    def start(self, run_id: str, payload: State) -> RunState:
        created = self.journal.begin_run(
            run_id,
            payload=payload,
            start_node=self.graph.START,
            activation_id=_new_id("act"),
            workflow_version=self.graph.version,
        )
        if created is None:
            events = self.journal.events(run_id)
            if not events or events[0].type is not EventType.RUN_STARTED:
                raise JournalLifecycleError(
                    f"existing run {run_id!r} has no valid run_started event"
                )
            recorded = events[0]
            if recorded.payload.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION:
                raise JournalLifecycleError(
                    f"existing run {run_id!r} uses a legacy journal schema"
                )
            if recorded.payload.get("workflow_version") != self.graph.version:
                raise JournalLifecycleError(
                    f"existing run {run_id!r} belongs to a different workflow version"
                )
            if recorded.payload.get("start_node") != self.graph.START:
                raise JournalLifecycleError(
                    f"existing run {run_id!r} belongs to a different workflow start"
                )
            if _canonical_fingerprint(recorded.payload.get("input")) != (
                _canonical_fingerprint(payload)
            ):
                raise JournalLifecycleError(
                    f"existing run {run_id!r} was started with different input"
                )
        return self.resume(run_id)

    def resume(self, run_id: str, *, step_limit: int | None = None) -> RunState:
        if step_limit is not None and step_limit < 0:
            raise ValueError("step_limit must not be negative")
        if not self.journal.events(run_id):
            raise JournalLifecycleError(f"run {run_id!r} does not exist")
        steps = 0
        while True:
            events = self.journal.events(run_id)
            rs = fold(self.graph, events)
            if rs.status in ("finished", "failed"):
                return rs
            if rs.next_node == Graph.END:
                self.journal.append(
                    run_id, EventType.RUN_FINISHED, None, {"state": rs.state}
                )
                return fold(self.graph, self.journal.events(run_id))
            if step_limit is not None and steps >= step_limit:
                return rs
            if rs.activation_id is None:
                raise JournalLifecycleError(
                    "running activation is missing activation_id"
                )

            node = self.graph.nodes[rs.next_node]
            if node.requires_approval and not rs.approval_granted:
                if rs.status == "awaiting_approval":
                    return rs
                self.journal.append(
                    run_id,
                    EventType.AWAITING_APPROVAL,
                    node.name,
                    {"state": rs.state},
                    activation_id=rs.activation_id,
                )
                return fold(self.graph, self.journal.events(run_id))

            recovery = rs.open_attempt_id is not None
            if rs.open_attempt_id is not None:
                self.journal.append(
                    run_id,
                    EventType.ATTEMPT_ABANDONED,
                    node.name,
                    {"reason": "recovery_resume"},
                    activation_id=rs.activation_id,
                    attempt_id=rs.open_attempt_id,
                )
            attempt_id = _new_id("att")
            self.journal.append(
                run_id,
                EventType.ATTEMPT_STARTED,
                node.name,
                {"recovery": recovery},
                activation_id=rs.activation_id,
                attempt_id=attempt_id,
            )
            events = self.journal.events(run_id)
            ctx = self._context(run_id, node.name, rs.activation_id, attempt_id, events)
            try:
                patch = node.fn(rs.state, ctx) or {}
                if not isinstance(patch, dict):
                    raise TypeError("node patch must be an object")
                routed_state = cast(State, {**rs.state, **patch})
                next_node = self.graph.next_after(node.name, routed_state)
                next_activation_id = None if next_node == Graph.END else _new_id("act")
                self.journal.append(
                    run_id,
                    EventType.ACTIVATION_FINISHED,
                    node.name,
                    {
                        "patch": patch,
                        "next_node": next_node,
                        "next_activation_id": next_activation_id,
                    },
                    activation_id=rs.activation_id,
                    attempt_id=attempt_id,
                )
            except Halt:
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self.journal.append(
                    run_id,
                    EventType.ATTEMPT_FAILED,
                    node.name,
                    {"error": reason},
                    activation_id=rs.activation_id,
                    attempt_id=attempt_id,
                )
                self.journal.append(
                    run_id, EventType.RUN_FAILED, node.name, {"reason": reason}
                )
                return fold(self.graph, self.journal.events(run_id))
            steps += 1

    def approve(self, run_id: str, *, granted: bool, by: str = "operator") -> RunState:
        rs = fold(self.graph, self.journal.events(run_id))
        if rs.status != "awaiting_approval" or rs.activation_id is None:
            raise ValueError(
                f"run {run_id} is not awaiting approval (status {rs.status})"
            )
        self.journal.append(
            run_id,
            EventType.APPROVAL_GRANTED if granted else EventType.APPROVAL_DENIED,
            rs.next_node,
            {"by": by, "at_ns": time.time_ns()},
            activation_id=rs.activation_id,
        )
        if not granted:
            self.journal.append(
                run_id,
                EventType.RUN_FAILED,
                rs.next_node,
                {"reason": "approval denied"},
            )
            return fold(self.graph, self.journal.events(run_id))
        return self.resume(run_id)

    def replay(self, run_id: str, upto_seq: int) -> RunState:

        return fold(self.graph, self.journal.events(run_id, upto_seq=upto_seq))
