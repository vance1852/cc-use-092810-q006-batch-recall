"""冻结组件谱系上的确定性遍历：批次扩散与有效所有权解析。

谱系证据只增不改（见 storage.lineage_revisions），本模块不访问数据库，
所有输入都是不可变的元组，保证范围版本可以由快照完整复算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence


class LineageError(ValueError):
    """谱系数据不满足遍历前提。"""


@dataclass(frozen=True, slots=True)
class AssetNode:
    asset_id: str
    kind: str
    capacity_kwh: Decimal
    top_level: bool


@dataclass(frozen=True, slots=True)
class LineageEdge:
    edge_id: str
    parent_id: str
    child_id: str
    relation: str
    effective_from: str


@dataclass(frozen=True, slots=True)
class OwnershipVersion:
    asset_id: str
    version: int
    holder_id: str
    kind: str
    effective_from: str


@dataclass(frozen=True)
class FrozenLineage:
    """某次范围计算所依据的不可变谱系与所有权快照。"""

    nodes: Mapping[str, AssetNode]
    edges: Sequence[LineageEdge]
    ownership: Mapping[str, Sequence[OwnershipVersion]]

    def adjacency(self) -> Mapping[str, Mapping[str, tuple[LineageEdge, ...]]]:
        forward: dict[str, list[LineageEdge]] = {}
        reverse: dict[str, list[LineageEdge]] = {}
        for edge in self.edges:
            if edge.parent_id not in self.nodes or edge.child_id not in self.nodes:
                raise LineageError(f"谱系边 {edge.edge_id} 引用了不存在的资产")
            forward.setdefault(edge.parent_id, []).append(edge)
            reverse.setdefault(edge.child_id, []).append(edge)
        return {
            "down": {key: tuple(sorted(value, key=_edge_sort)) for key, value in forward.items()},
            "up": {key: tuple(sorted(value, key=_edge_sort)) for key, value in reverse.items()},
        }


def _edge_sort(edge: LineageEdge) -> tuple[str, str]:
    return edge.effective_from, edge.edge_id


def _visit(
    lineage: FrozenLineage,
    start: str,
    direction: str,
) -> frozenset[str]:
    """沿指定方向传播，visited 同时充当有向环防护（环上资产只取一次）。"""

    if start not in lineage.nodes:
        raise LineageError(f"批次起点 {start} 不在谱系中")
    ways = ("down", "up") if direction == "both" else (
        "down" if direction in ("down", "downstream") else "up",
    )
    graph = lineage.adjacency()
    visited: set[str] = {start}
    for way in ways:
        stack = [start]
        seen_this_way = {start}
        while stack:
            current = stack.pop()
            edges = graph[way].get(current, ())
            for edge in edges:
                nxt = edge.child_id if way == "down" else edge.parent_id
                if nxt in seen_this_way:
                    continue  # 有向环：不再展开，不报错也不重复计数
                seen_this_way.add(nxt)
                visited.add(nxt)
                stack.append(nxt)
    return frozenset(visited)


def spread_from_seeds(
    lineage: FrozenLineage,
    seeds: Sequence[str],
    direction: str,
) -> frozenset[str]:
    """从冻结批次起点按证据方向扩散，返回去重后的受影响资产集合。"""

    if not seeds:
        raise LineageError("初始范围至少需要一个批次起点")
    affected: set[str] = set()
    for seed in seeds:
        affected.update(_visit(lineage, seed, direction))
    return frozenset(affected)


def effective_owner(
    lineage: FrozenLineage,
    asset_id: str,
    as_of: str,
) -> OwnershipVersion | None:
    """取生效时间不晚于 as_of 的最新所有权版本；没有任何版本时返回 None。"""

    versions = sorted(
        lineage.ownership.get(asset_id, ()),
        key=lambda item: (item.effective_from, item.version),
    )
    effective: OwnershipVersion | None = None
    for item in versions:
        if item.effective_from <= as_of:
            effective = item
    return effective
