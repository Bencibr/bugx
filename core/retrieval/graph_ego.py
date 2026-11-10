"""Bounded ego-subgraph extraction for project relationship graphs.

The response's canonical JSON representation is kept below 800 UTF-8 bytes.
That byte limit is a conservative upper bound for tokenizers that operate on
UTF-8 text, so a graph response cannot grow past its serialized token budget.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
import json
from typing import Any, Dict, List, Optional, Tuple


MAX_SERIALIZED_BYTES = 799
MAX_RESPONSE_NODES = 32
MAX_RESPONSE_EDGES = 64
_GRAPH_INDEX_CACHE_SIZE = 8


@dataclass(frozen=True)
class _GraphIndex:
    """Normalized graph data shared by repeated ego queries.

    The adjacency tuples are built once and then reused by all queries for the
    same graph.  Records remain ordinary dictionaries so the response packer
    can retain the existing metadata/truncation semantics; callers only ever
    receive shallow copies of those records.
    """

    node_by_id: Dict[str, Dict[str, Any]]
    edges: Tuple[Dict[str, Any], ...]
    outgoing: Dict[str, Tuple[Dict[str, Any], ...]]
    incoming: Dict[str, Tuple[Dict[str, Any], ...]]
    edge_positions: Dict[int, int]


class _GraphCacheKey:
    """Hashable identity/shape key for an unhashable graph mapping.

    Graphs are assembled once and treated as snapshots by the retrieval
    pipeline.  The node/edge container identities and lengths are included so
    replacing or appending to either container naturally gets a fresh index,
    while cache-key creation remains O(1) for large graphs.
    """

    __slots__ = (
        "graph",
        "nodes_identity",
        "edges_identity",
        "nodes_size",
        "edges_size",
    )

    def __init__(
        self,
        graph: Mapping[str, Any],
        nodes: Any,
        edges: Any,
        nodes_size: int,
        edges_size: int,
    ) -> None:
        self.graph = graph
        self.nodes_identity = id(nodes)
        self.edges_identity = id(edges)
        self.nodes_size = nodes_size
        self.edges_size = edges_size

    def __hash__(self) -> int:
        return hash(
            (
                id(self.graph),
                self.nodes_identity,
                self.edges_identity,
                self.nodes_size,
                self.edges_size,
            )
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _GraphCacheKey):
            return NotImplemented
        return (
            self.graph is other.graph
            and self.nodes_identity == other.nodes_identity
            and self.edges_identity == other.edges_identity
            and self.nodes_size == other.nodes_size
            and self.edges_size == other.edges_size
        )


@dataclass(frozen=True)
class GraphEgoResponse:
    """A depth-limited graph neighborhood and its canonical JSON encoding."""

    center_node_id: Optional[str] = None
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    hops: Dict[str, int] = field(default_factory=dict)
    depth: int = 1
    direction: str = "both"
    complete: bool = True
    truncated: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-ready mapping without exposing mutable internals."""
        result: Dict[str, Any] = {
            "center_node_id": self.center_node_id,
            "depth": self.depth,
            "direction": self.direction,
            "nodes": deepcopy(self.nodes),
            "edges": deepcopy(self.edges),
            "hops": dict(self.hops),
            "complete": self.complete,
            "truncated": self.truncated,
        }
        if self.error is not None:
            result["error"] = self.error
        return result

    def to_json(self) -> str:
        """Serialize compactly and deterministically for budget accounting."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def serialized_bytes(self) -> int:
        return len(self.to_json().encode("utf-8"))

    @property
    def serialized_tokens(self) -> int:
        """Conservative token bound: at most one token per serialized byte."""
        return self.serialized_bytes


def _edge_key(edge: Mapping[str, Any]) -> Tuple[str, str, Any]:
    relation_type = edge.get("type")
    try:
        hash(relation_type)
    except TypeError:
        relation_type = repr(relation_type)
    return (edge["source"], edge["target"], relation_type)


def _iter_node_items(graph: Mapping[str, Any]) -> List[Any]:
    """Normalize the supported node containers without changing validation."""
    raw_nodes = graph.get("nodes", ())
    if isinstance(raw_nodes, Mapping):
        node_items = []
        for node_id, value in raw_nodes.items():
            if isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("id", node_id)
            else:
                item = {"id": node_id, "value": value}
            node_items.append(item)
        return node_items

    if raw_nodes is None:
        return []
    if isinstance(raw_nodes, (str, bytes, bytearray)):
        raise TypeError("graph nodes must be an iterable of node records")
    try:
        return list(raw_nodes)
    except TypeError as exc:
        raise TypeError("graph nodes must be iterable") from exc


def _build_graph_index(graph: Mapping[str, Any]) -> _GraphIndex:
    """Build the normalized, bidirectional adjacency index for ``graph``."""
    node_by_id: Dict[str, Dict[str, Any]] = {}
    for node in _iter_node_items(graph):
        if isinstance(node, Mapping) and isinstance(node.get("id"), str):
            node_by_id.setdefault(node["id"], dict(node))

    raw_edges = graph.get("edges", ())
    if raw_edges is None:
        raw_edges = ()
    if isinstance(raw_edges, (str, bytes, bytearray)):
        raise TypeError("graph edges must be an iterable of edge records")
    try:
        edge_items = iter(raw_edges)
    except TypeError as exc:
        raise TypeError("graph edges must be iterable") from exc

    edges: List[Dict[str, Any]] = []
    seen_edges = set()
    outgoing: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    incoming: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for edge in edge_items:
        if not isinstance(edge, Mapping):
            continue
        source, target = edge.get("source"), edge.get("target")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in node_by_id
            or target not in node_by_id
        ):
            continue
        normalized = dict(edge)
        key = _edge_key(normalized)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(normalized)
        outgoing[source].append(normalized)
        incoming[target].append(normalized)

    edge_tuple = tuple(edges)
    return _GraphIndex(
        node_by_id=node_by_id,
        edges=edge_tuple,
        outgoing={node_id: tuple(values) for node_id, values in outgoing.items()},
        incoming={node_id: tuple(values) for node_id, values in incoming.items()},
        edge_positions={id(edge): position for position, edge in enumerate(edge_tuple)},
    )


def _cache_container_size(value: Any) -> Optional[int]:
    """Return a cheap cache-shape size, or ``None`` for one-shot iterables."""
    if value is None:
        return 0
    if not isinstance(value, (Mapping, list, tuple)):
        return None
    try:
        return len(value)
    except TypeError:
        return None


def _graph_cache_key(graph: Mapping[str, Any]) -> Optional[_GraphCacheKey]:
    """Create an O(1) key for reusable graph containers.

    Generators and other one-shot iterables deliberately bypass the cache so
    their consumption semantics remain identical to the original function.
    """
    nodes = graph.get("nodes", ())
    edges = graph.get("edges", ())
    nodes_size = _cache_container_size(nodes)
    edges_size = _cache_container_size(edges)
    if nodes_size is None or edges_size is None:
        return None
    return _GraphCacheKey(graph, nodes, edges, nodes_size, edges_size)


@lru_cache(maxsize=_GRAPH_INDEX_CACHE_SIZE)
def _cached_graph_index(key: _GraphCacheKey) -> _GraphIndex:
    return _build_graph_index(key.graph)


def _get_graph_index(graph: Mapping[str, Any]) -> _GraphIndex:
    key = _graph_cache_key(graph)
    if key is None:
        return _build_graph_index(graph)
    return _cached_graph_index(key)


def _minimal_node(node_id: str) -> Dict[str, Any]:
    return {"id": node_id}


def _minimal_edge(edge: Mapping[str, Any]) -> Dict[str, Any]:
    compact = {
        "source": edge["source"],
        "target": edge["target"],
    }
    if "type" in edge:
        compact["type"] = edge["type"]
    return compact


def _safe_empty_response(
    center_node_id: str,
    depth: int,
    direction: str,
    *,
    error: Optional[str] = None,
) -> GraphEgoResponse:
    response = GraphEgoResponse(
        center_node_id=center_node_id,
        depth=depth,
        direction=direction,
        error=error,
    )
    if response.serialized_bytes < 800:
        return response
    # An unbounded caller-supplied ID must not bypass the serialized response
    # limit. Preserve the failure state and make the unrepresentable ID explicit.
    return GraphEgoResponse(
        center_node_id=None,
        depth=depth,
        direction=direction,
        complete=False,
        truncated=True,
        error="serialization_budget_exceeded",
    )


def _extract_ego_subgraph_uncached(
    graph: Mapping[str, Any],
    center_node_id: str,
    depth: int = 1,
    direction: str = "both",
) -> GraphEgoResponse:
    """Return the center node's bounded in/out/bidirectional neighborhood.

    ``graph`` uses the ``nodes``/``edges`` shape produced by
    :func:`core.project_graph.build_graph`: node records have an ``id`` and
    edge records have ``source``/``target`` IDs. The center and each newly
    reached node are kept ahead of optional metadata when the byte budget
    requires truncation. Every returned edge has both endpoints in ``nodes``.
    """
    if not isinstance(graph, Mapping):
        raise TypeError("graph must be a mapping")
    if not isinstance(center_node_id, str):
        raise TypeError("center_node_id must be a string")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth not in (1, 2):
        raise ValueError("depth must be 1 or 2")
    if direction not in ("both", "inbound", "outbound"):
        raise ValueError("direction must be 'both', 'inbound', or 'outbound'")

    index = _get_graph_index(graph)
    if center_node_id not in index.node_by_id:
        return _safe_empty_response(
            center_node_id, depth, direction, error="not_found",
        )

    node_order = [center_node_id]
    hops = {center_node_id: 0}
    parent_edges: Dict[str, Dict[str, Any]] = {}
    queue = deque([center_node_id])
    truncated = False

    while queue:
        current = queue.popleft()
        current_hop = hops[current]
        if current_hop >= depth:
            continue
        if direction == "outbound":
            incident = index.outgoing.get(current, ())
        elif direction == "inbound":
            incident = index.incoming.get(current, ())
        else:
            incident = (*index.outgoing.get(current, ()), *index.incoming.get(current, ()))

        for edge in incident:
            source, target = edge["source"], edge["target"]
            if direction == "outbound":
                neighbor = target
            elif direction == "inbound":
                neighbor = source
            else:
                neighbor = target if source == current else source

            if neighbor in hops:
                continue
            if len(node_order) >= MAX_RESPONSE_NODES:
                truncated = True
                queue.clear()
                break
            hops[neighbor] = current_hop + 1
            node_order.append(neighbor)
            parent_edges[neighbor] = edge
            queue.append(neighbor)

    selected_ids = set(node_order)
    selected_edges: List[Dict[str, Any]] = []
    selected_edge_ids = set()
    # Pull candidates through both adjacency directions instead of scanning
    # every edge in the graph.  Sorting by the original position preserves
    # the deterministic edge order of the pre-index implementation.
    for node_id in node_order:
        for incident in (
            index.outgoing.get(node_id, ()),
            index.incoming.get(node_id, ()),
        ):
            for edge in incident:
                edge_id = id(edge)
                if edge_id in selected_edge_ids:
                    continue
                if edge["source"] in selected_ids and edge["target"] in selected_ids:
                    selected_edge_ids.add(edge_id)
                    selected_edges.append(edge)
    selected_edges.sort(key=lambda edge: index.edge_positions[id(edge)])
    if len(selected_edges) > MAX_RESPONSE_EDGES:
        selected_edges = selected_edges[:MAX_RESPONSE_EDGES]
        truncated = True

    # Build in BFS order. Each neighbor and its discovery edge are packed as a
    # unit so truncation cannot leave a dangling or disconnected frontier node.
    packed_nodes: List[Dict[str, Any]] = []
    packed_edges: List[Dict[str, Any]] = []
    packed_hops: Dict[str, int] = {}
    included = set()

    def fits(
        nodes: List[Dict[str, Any]],
        response_edges: List[Dict[str, Any]],
        response_hops: Dict[str, int],
    ) -> bool:
        probe = GraphEgoResponse(
            center_node_id=center_node_id,
            nodes=nodes,
            edges=response_edges,
            hops=response_hops,
            depth=depth,
            direction=direction,
            complete=False,
            truncated=True,
        )
        try:
            return probe.serialized_bytes < 800
        except (TypeError, ValueError):
            return False

    center_record = dict(index.node_by_id[center_node_id])
    if fits([center_record], [], {center_node_id: 0}):
        packed_nodes.append(center_record)
        packed_hops[center_node_id] = 0
        included.add(center_node_id)
    else:
        compact_center = _minimal_node(center_node_id)
        if not fits([compact_center], [], {center_node_id: 0}):
            return _safe_empty_response(
                center_node_id,
                depth,
                direction,
                error="serialization_budget_exceeded",
            )
        packed_nodes.append(compact_center)
        packed_hops[center_node_id] = 0
        included.add(center_node_id)
        truncated = True

    for node_id in node_order[1:]:
        parent_edge = parent_edges[node_id]
        parent_id = (
            parent_edge["source"]
            if parent_edge["target"] == node_id
            else parent_edge["target"]
        )
        if parent_id not in included:
            truncated = True
            continue

        full_node = dict(index.node_by_id[node_id])
        full_edge = dict(parent_edge)
        node_candidates = [full_node, _minimal_node(node_id)]
        edge_candidates = [full_edge, _minimal_edge(parent_edge)]
        packed = False
        for node_record in node_candidates:
            if packed:
                break
            for edge_record in edge_candidates:
                candidate_nodes = [*packed_nodes, node_record]
                candidate_edges = [*packed_edges, edge_record]
                candidate_hops = {**packed_hops, node_id: hops[node_id]}
                if fits(candidate_nodes, candidate_edges, candidate_hops):
                    packed_nodes = candidate_nodes
                    packed_edges = candidate_edges
                    packed_hops = candidate_hops
                    included.add(node_id)
                    packed = True
                    if node_record is not full_node or edge_record is not full_edge:
                        truncated = True
                    break
        if not packed:
            truncated = True

    included_edge_keys = {_edge_key(edge) for edge in packed_edges}
    for edge in selected_edges:
        key = _edge_key(edge)
        if key in included_edge_keys or edge["source"] not in included or edge["target"] not in included:
            continue
        if len(packed_edges) >= MAX_RESPONSE_EDGES:
            truncated = True
            break
        full_edge = dict(edge)
        for edge_record in (full_edge, _minimal_edge(edge)):
            candidate_edges = [*packed_edges, edge_record]
            if fits(packed_nodes, candidate_edges, packed_hops):
                packed_edges = candidate_edges
                included_edge_keys.add(key)
                if edge_record is not full_edge:
                    truncated = True
                break
        else:
            truncated = True

    response = GraphEgoResponse(
        center_node_id=center_node_id,
        nodes=packed_nodes,
        edges=packed_edges,
        hops=packed_hops,
        depth=depth,
        direction=direction,
        complete=not truncated,
        truncated=truncated,
    )
    # The fit probes reserve space for truncated/complete metadata, so this
    # assertion guards future edits to the response envelope.
    if response.serialized_bytes >= 800:
        return _safe_empty_response(
            center_node_id,
            depth,
            direction,
            error="serialization_budget_exceeded",
        )
    return response


@lru_cache(maxsize=32)
def _cached_ego_response(
    key: _GraphCacheKey,
    center_node_id: str,
    depth: int,
    direction: str,
) -> GraphEgoResponse:
    """Cache a bounded query result after the graph index is normalized."""
    return _extract_ego_subgraph_uncached(
        key.graph,
        center_node_id,
        depth=depth,
        direction=direction,
    )


def _copy_response(response: GraphEgoResponse) -> GraphEgoResponse:
    """Return an isolated response copy for callers of the result cache."""
    return GraphEgoResponse(
        center_node_id=response.center_node_id,
        nodes=deepcopy(response.nodes),
        edges=deepcopy(response.edges),
        hops=dict(response.hops),
        depth=response.depth,
        direction=response.direction,
        complete=response.complete,
        truncated=response.truncated,
        error=response.error,
    )


def extract_ego_subgraph(
    graph: Mapping[str, Any],
    center_node_id: str,
    depth: int = 1,
    direction: str = "both",
) -> GraphEgoResponse:
    """Return a bounded ego graph using cached graph indexes and query results."""
    # Keep request validation outside the cache so invalid calls never occupy a
    # cache slot and preserve the original exception types/messages.
    if not isinstance(graph, Mapping):
        raise TypeError("graph must be a mapping")
    if not isinstance(center_node_id, str):
        raise TypeError("center_node_id must be a string")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth not in (1, 2):
        raise ValueError("depth must be 1 or 2")
    if direction not in ("both", "inbound", "outbound"):
        raise ValueError("direction must be 'both', 'inbound', or 'outbound'")

    key = _graph_cache_key(graph)
    if key is None:
        return _extract_ego_subgraph_uncached(
            graph,
            center_node_id,
            depth=depth,
            direction=direction,
        )
    return _copy_response(
        _cached_ego_response(key, center_node_id, depth, direction),
    )


def _clear_caches() -> None:
    """Clear both bounded caches; useful for lifecycle/test isolation."""
    _cached_ego_response.cache_clear()
    _cached_graph_index.cache_clear()


# Keep cache introspection available on the public callable without making the
# unhashable graph argument part of functools' cache key.
extract_ego_subgraph.cache_info = _cached_ego_response.cache_info  # type: ignore[attr-defined]
extract_ego_subgraph.cache_clear = _clear_caches  # type: ignore[attr-defined]


__all__ = ["GraphEgoResponse", "extract_ego_subgraph"]
