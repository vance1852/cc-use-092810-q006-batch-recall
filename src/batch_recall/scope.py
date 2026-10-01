"""范围版本的纯计算逻辑：冻结谱系闭包、有效所有权与版本内容。

所有函数均无副作用：给定相同的谱系边、所有权版本和父版本，
任一范围版本都可以被审计人员独立复算。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .contracts import STAGE_INDEX
from .errors import ValidationFailed


@dataclass(frozen=True, slots=True)
class Edge:
    edge_id: str
    parent_id: str
    child_id: str
    edge_kind: str
    observed_at: str
    evidence_sha256: str
    genealogy_revision: int


def edge_from_row(row: Mapping[str, Any]) -> Edge:
    return Edge(
        edge_id=row["edge_id"],
        parent_id=row["parent_id"],
        child_id=row["child_id"],
        edge_kind=row["edge_kind"],
        observed_at=row["observed_at"],
        evidence_sha256=row["evidence_sha256"],
        genealogy_revision=int(row["genealogy_revision"]),
    )


def impacted_components(edges: Sequence[Edge], seed: str) -> dict[str, list[dict[str, str]]]:
    """沿谱系边双向扩散，返回种子批次可达的全部资产及最短证据路径。

    返回值键为资产编号，值为从种子出发的路径（含种子本身）。
    风险既可随装入关系向下游电池包扩散，也可随拆分/返修向上游回溯。
    """

    adjacency: dict[str, list[tuple[str, Edge]]] = {}
    for edge in edges:
        adjacency.setdefault(edge.parent_id, []).append((edge.child_id, edge))
        adjacency.setdefault(edge.child_id, []).append((edge.parent_id, edge))
    paths: dict[str, list[dict[str, str]]] = {seed: [{"asset_id": seed}]}
    if seed not in adjacency:
        return paths
    queue: deque[str] = deque([seed])
    while queue:
        current = queue.popleft()
        for neighbor, edge in adjacency[current]:
            if neighbor in paths:
                continue
            step = {
                "asset_id": neighbor,
                "edge_id": edge.edge_id,
                "edge_kind": edge.edge_kind,
                "via": "child" if edge.parent_id == current else "parent",
            }
            paths[neighbor] = paths[current] + [step]
            queue.append(neighbor)
    return paths


def effective_owners(
    ownership_rows: Sequence[Mapping[str, Any]], as_of: str
) -> dict[str, dict[str, Any]]:
    """返回 as_of 时刻每个资产的最新有效所有权版本。"""

    latest: dict[str, Mapping[str, Any]] = {}
    for row in ownership_rows:
        if row["effective_at"] > as_of:
            continue
        current = latest.get(row["asset_id"])
        if current is None or int(row["version"]) > int(current["version"]):
            latest[row["asset_id"]] = row
    return {
        asset_id: {
            "holder_id": row["holder_id"],
            "mode": row["mode"],
            "version": int(row["version"]),
            "effective_at": row["effective_at"],
        }
        for asset_id, row in latest.items()
    }


def _sorted_entries(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(payload, key=lambda item: item["asset_id"])


def build_entries(
    *,
    change_kind: str,
    seed: str,
    edges: Sequence[Edge],
    parents: Mapping[str, Mapping[str, Any]],
    excluded: frozenset[str] | None,
    scope_version: int,
) -> list[dict[str, Any]]:
    """根据变更类型构造版本明细。

    parents 为上一有效版本的明细（asset_id -> 行）；缩减只允许排除
    已在父版本中的资产，且保留首次通知事实与证据路径。
    """

    if change_kind in {"initial", "expand"}:
        impacted = impacted_components(edges, seed)
        entries: list[dict[str, Any]] = []
        for asset_id in sorted(impacted):
            previous = parents.get(asset_id)
            if previous is not None and previous["include_state"] == "included":
                entries.append({
                    "asset_id": asset_id,
                    "include_state": "included",
                    "added_in_version": int(previous["added_in_version"]),
                    "match_path": previous["match_path"],
                    "first_noticed_at": previous.get("first_noticed_at"),
                })
            else:
                entries.append({
                    "asset_id": asset_id,
                    "include_state": "included",
                    "added_in_version": scope_version,
                    "match_path": impacted[asset_id],
                    "first_noticed_at": None,
                })
        return _sorted_entries(entries)

    if change_kind == "reduce":
        excluded = excluded or frozenset()
        unknown = sorted(asset_id for asset_id in excluded if asset_id not in parents)
        if unknown:
            raise ValidationFailed(f"缩减资产不在当前范围内: {', '.join(unknown)}")
        if not excluded:
            raise ValidationFailed("范围缩减必须指定至少一项资产")
        entries = []
        for asset_id, previous in sorted(parents.items()):
            if asset_id in excluded and previous["include_state"] == "included":
                entries.append({
                    "asset_id": asset_id,
                    "include_state": "excluded",
                    "added_in_version": int(previous["added_in_version"]),
                    "match_path": previous["match_path"],
                    "first_noticed_at": previous.get("first_noticed_at"),
                })
            else:
                entries.append({
                    "asset_id": asset_id,
                    "include_state": previous["include_state"],
                    "added_in_version": int(previous["added_in_version"]),
                    "match_path": previous["match_path"],
                    "first_noticed_at": previous.get("first_noticed_at"),
                })
        return entries

    raise ValidationFailed(f"未知范围变更类型: {change_kind}")


def scope_content(
    *,
    recall_id: str,
    scope_version: int,
    change_kind: str,
    genealogy_revision: int,
    as_of: str,
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """版本的规范化内容；审计复算时应逐字段一致。"""

    return {
        "recall_id": recall_id,
        "scope_version": scope_version,
        "change_kind": change_kind,
        "genealogy_revision": genealogy_revision,
        "as_of": as_of,
        "entries": [
            {
                "asset_id": row["asset_id"],
                "include_state": row["include_state"],
                "added_in_version": int(row["added_in_version"]),
                "match_path": row["match_path"],
            }
            for row in entries
        ],
    }


def stage_progress(completed_stages: Sequence[str]) -> int:
    """已完成阶段中的最高序号；措施只允许顺序前移。"""

    if not completed_stages:
        return -1
    return max(STAGE_INDEX[stage] for stage in completed_stages)


def quantize_capacity(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.001")), "f")
