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
    run_id: str
    node: str
    activation_id: str
    attempt_id: str
    call: EffectCall


@dataclass(frozen=True)
class Node:
    name: str
    fn: NodeFn
    requires_approval: bool = False
    description: str = ""


@dataclass(frozen=True)
class StaticRoute:
    target: str


Route = StaticRoute | Callable[[State], str]


@dataclass
class Graph:
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

        route = self.routes.get(node)
        if route is None:
            return self.END
        target = route.target if isinstance(route, StaticRoute) else route(state)
        if target != self.END and target not in self.nodes:
            raise ValueError(f"route from {node!r} selected undefined node {target!r}")
        return target

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.START not in self.nodes:
            problems.append(f"START node {self.START!r} is not defined")
        for name in self.nodes:
            if name not in self.routes:
                problems.append(f"node {name!r} has no outgoing route")
        for frm, route in self.routes.items():
            if frm not in self.nodes:
                problems.append(f"route from undefined node {frm!r}")
            # Branches can't be checked without calling user code.
            if isinstance(route, StaticRoute):
                target = route.target
                if target != self.END and target not in self.nodes:
                    problems.append(f"edge {frm!r} -> {target!r} points nowhere")
        return problems
