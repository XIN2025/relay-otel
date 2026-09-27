"""Graph definition. Nodes do work, edges decide what runs next.

A node is a pure-ish function of the accumulated state. It receives the state and
returns a patch to merge into it. That shape is what makes replay possible: given
the same state and the same recorded side effects, a node produces the same patch,
so re-running a prefix of the graph reconstructs the same state every time.

Side effects that are *not* reproducible (an LLM call, an HTTP request) do not
belong inside a node directly. They go through ``ctx.call``, which records an
intent before invocation and a completion afterward. A completed result can be
resolved from the journal; an incomplete, non-idempotent intent fails closed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, TypeVar

from .types import Document
from .types import State as State

ResultT = TypeVar("ResultT")


class EffectCall(Protocol):
    def __call__(
        self,
        label: str,
        fn: Callable[[], ResultT],
        *,
        request: object = None,
        adapter_version: str = "unspecified",
        effect_id: str | None = None,
        idempotency_key: str | None = None,
        retry_safe: bool = False,
    ) -> ResultT: ...


class NodeFn(Protocol):
    def __call__(self, state: State, ctx: NodeContext) -> Document: ...


@dataclass
class NodeContext:
    """What a node is allowed to reach. Deliberately narrow.

    `call` is the only door to the outside world. Anything a node does through it
    has an explicit durable intent and outcome; anything it does around it is not
    protected, which is why this is the whole surface rather than handing nodes a
    client.
    """

    run_id: str
    node: str
    activation_id: str
    attempt_id: str
    call: EffectCall


@dataclass(frozen=True)
class Node:
    name: str
    fn: NodeFn
    # A node marked `requires_approval` halts the run before it executes. The run
    # is not held in memory while it waits: it ends, and resuming is an ordinary
    # resume from the journal. A pause that only survives while the process lives
    # is not a human-in-the-loop checkpoint, it is a blocking prompt.
    requires_approval: bool = False
    description: str = ""


@dataclass(frozen=True)
class StaticRoute:
    target: str


Route = StaticRoute | Callable[[State], str]


@dataclass
class Graph:
    """Nodes plus the routing between them.

    Routing is a function of state rather than a static edge list, because the
    interesting agent graphs branch on what a step produced. `END` terminates.
    """

    START: str
    version: str = "unversioned"
    nodes: dict[str, Node] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)

    END: ClassVar[str] = "__end__"

    def node(
        self, name: str, *, requires_approval: bool = False, description: str = ""
    ) -> Callable[[NodeFn], NodeFn]:
        def register(fn: NodeFn) -> NodeFn:
            if name in self.nodes:
                raise ValueError(f"node {name!r} is already defined")
            self.nodes[name] = Node(
                name=name,
                fn=fn,
                requires_approval=requires_approval,
                description=description or (fn.__doc__ or "").strip().split("\n")[0],
            )
            return fn

        return register

    def edge(self, frm: str, to: str) -> None:
        self._register_route(frm, StaticRoute(to))

    def branch(self, frm: str, chooser: Callable[[State], str]) -> None:
        self._register_route(frm, chooser)

    def _register_route(self, frm: str, route: Route) -> None:
        if frm in self.routes:
            raise ValueError(f"route from {frm!r} is already defined")
        self.routes[frm] = route

    def next_after(self, node: str, state: State) -> str:
        """Choose a route during live execution.

        The chosen target is persisted in ``activation_finished``. Folding and
        replaying a run never call this method, so a user chooser cannot fire
        while an observer reconstructs history.
        """

        route = self.routes.get(node)
        if route is None:
            return self.END
        target = route.target if isinstance(route, StaticRoute) else route(state)
        if target != self.END and target not in self.nodes:
            raise ValueError(f"route from {node!r} selected undefined node {target!r}")
        return target

    def validate(self) -> list[str]:
        """Structural problems worth catching before a run rather than during one."""
        problems: list[str] = []
        if self.START not in self.nodes:
            problems.append(f"START node {self.START!r} is not defined")
        for name in self.nodes:
            if name not in self.routes:
                problems.append(f"node {name!r} has no outgoing route")
        for frm, route in self.routes.items():
            if frm not in self.nodes:
                problems.append(f"route from undefined node {frm!r}")
            # A static edge is checkable without running it; a branch is not, and
            # pretending otherwise would mean calling user code during validation.
            if isinstance(route, StaticRoute):
                target = route.target
                if target != self.END and target not in self.nodes:
                    problems.append(f"edge {frm!r} -> {target!r} points nowhere")
        return problems
