"""Cycle analysis for Attack Flow graphs.

Both algorithms are iterative (the largest corpus flow has thousands of
objects, so recursion is off the table) and deterministic: neighbours are
visited in sorted id order and roots in the order given.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.attack_flow.model import CONTROL_TYPES, AttackFlow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleReport:
    components: list[list[str]]  # SCCs with >1 node, or a node with a self-loop
    back_edges: list[tuple[str, str]]  # edges removed to get the acyclic view


def _adjacency(
    nodes: Sequence[str], edges: Sequence[tuple[str, str]]
) -> dict[str, list[str]]:
    known = set(nodes)
    sets: dict[str, set[str]] = {node: set() for node in known}
    for src, dst in edges:
        if src in known and dst in known:
            sets[src].add(dst)
    return {node: sorted(targets) for node, targets in sets.items()}


def _tarjan(order: list[str], adj: dict[str, list[str]]) -> list[list[str]]:
    """Iterative Tarjan; returns every SCC as a list of node ids."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    for start in order:
        if start in index_of:
            continue
        index_of[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, pos = work.pop()
            neighbours = adj[node]
            if pos < len(neighbours):
                work.append((node, pos + 1))
                nxt = neighbours[pos]
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
                continue
            # All neighbours done: close the node.
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                sccs.append(component)
    return sccs


def _back_edges(order: list[str], adj: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Iterative DFS; an edge reaching a node still on the DFS path is a back edge."""
    state: dict[str, int] = {}  # 1 = on path, 2 = finished
    found: list[tuple[str, str]] = []
    for start in order:
        if start in state:
            continue
        state[start] = 1
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, pos = work.pop()
            neighbours = adj[node]
            if pos < len(neighbours):
                work.append((node, pos + 1))
                nxt = neighbours[pos]
                seen = state.get(nxt)
                if seen is None:
                    state[nxt] = 1
                    work.append((nxt, 0))
                elif seen == 1:
                    found.append((node, nxt))
                continue
            state[node] = 2
    return found


def analyze_cycles(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
    roots: Sequence[str],
) -> CycleReport:
    """Find cyclic components and the back edges that make the graph acyclic.

    Roots are visited first (in the given order), then the remaining nodes in
    sorted id order, so the same input always yields the same report.
    """
    adj = _adjacency(nodes, edges)
    root_order = [r for r in dict.fromkeys(roots) if r in adj]
    rest = sorted(n for n in adj if n not in set(root_order))
    order = root_order + rest

    components: list[list[str]] = []
    for scc in _tarjan(order, adj):
        if len(scc) > 1 or scc[0] in adj[scc[0]]:
            components.append(sorted(scc))
    components.sort()
    return CycleReport(components=components, back_edges=_back_edges(order, adj))


def annotate_flow_cycles(flow: AttackFlow) -> AttackFlow:
    """Return a copy of ``flow`` with ``cycles``/``back_edges`` filled in.

    Logs a warning with the object ids involved; never raises on cycles.
    """
    nodes = [o.id for o in flow.objects if o.type in CONTROL_TYPES]
    report = analyze_cycles(nodes, flow.edges(), flow.start_refs)
    if report.components:
        logger.warning(
            "attack flow %s has cycles: %s (back edges removed for execution: %s)",
            flow.flow_id,
            report.components,
            report.back_edges,
        )
    return flow.model_copy(
        update={
            "cycles": report.components,
            "back_edges": report.back_edges,
        }
    )
