from __future__ import annotations

import unittest
from decimal import Decimal

from recall_orchestration.lineage import (
    AssetNode,
    FrozenLineage,
    LineageEdge,
    LineageError,
    OwnershipVersion,
    effective_owner,
    spread_from_seeds,
)
from recall_orchestration.scope import ScopeRequest, compute_scope


def node(asset_id: str, *, kind: str = "pack", capacity: str = "100", top: bool = True) -> AssetNode:
    return AssetNode(asset_id, kind, Decimal(capacity), top)


def edge(edge_id: str, parent: str, child: str, *, relation: str = "contained",
         when: str = "2026-09-01T00:00:00Z") -> LineageEdge:
    return LineageEdge(edge_id, parent, child, relation, when)


def owner(asset_id: str, version: int, holder: str, when: str, *, kind: str = "sale") -> OwnershipVersion:
    return OwnershipVersion(asset_id, version, holder, kind, when)


def lineage(nodes, edges, ownership=()) -> FrozenLineage:
    owners: dict[str, list[OwnershipVersion]] = {}
    for item in ownership:
        owners.setdefault(item.asset_id, []).append(item)
    return FrozenLineage(nodes={n.asset_id: n for n in nodes}, edges=tuple(edges), ownership=owners)


class LineageTraversalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = lineage(
            [node("lot", kind="cell_lot", capacity="0", top=False),
             node("mod", kind="module", capacity="0", top=False),
             node("pack"), node("rack")],
            [edge("e1", "lot", "mod", relation="manufactured_from"),
             edge("e2", "mod", "pack", relation="contained"),
             edge("e3", "pack", "rack", relation="installed_in")],
        )

    def test_downstream_spread(self) -> None:
        self.assertEqual(spread_from_seeds(self.graph, ["lot"], "downstream"),
                         frozenset({"lot", "mod", "pack", "rack"}))

    def test_upstream_spread(self) -> None:
        self.assertEqual(spread_from_seeds(self.graph, ["rack"], "upstream"),
                         frozenset({"lot", "mod", "pack", "rack"}))

    def test_both_spread_from_middle(self) -> None:
        self.assertEqual(spread_from_seeds(self.graph, ["mod"], "both"),
                         frozenset({"lot", "mod", "pack", "rack"}))

    def test_diamond_is_deduplicated(self) -> None:
        graph = lineage(
            [node("lot", capacity="0", top=False), node("m1", capacity="0", top=False),
             node("m2", capacity="0", top=False), node("pack")],
            [edge("a", "lot", "m1"), edge("b", "lot", "m2"),
             edge("c", "m1", "pack"), edge("d", "m2", "pack")],
        )
        result = spread_from_seeds(graph, ["lot"], "downstream")
        self.assertEqual(result, frozenset({"lot", "m1", "m2", "pack"}))

    def test_directed_cycle_does_not_loop_or_double_count(self) -> None:
        graph = lineage(
            [node("a"), node("b"), node("c")],
            [edge("a-b", "a", "b", relation="manufactured_from"),
             edge("b-c", "b", "c", relation="installed_in"),
             edge("c-a", "c", "a", relation="removed_from")],
        )
        result = spread_from_seeds(graph, ["a"], "downstream")
        self.assertEqual(result, frozenset({"a", "b", "c"}))

    def test_unknown_seed_rejected(self) -> None:
        with self.assertRaises(LineageError):
            spread_from_seeds(self.graph, ["ghost"], "downstream")

    def test_empty_seeds_rejected(self) -> None:
        with self.assertRaises(LineageError):
            spread_from_seeds(self.graph, [], "downstream")

    def test_edge_to_unknown_node_rejected(self) -> None:
        graph = lineage([node("a")], [edge("x", "a", "ghost")])
        with self.assertRaises(LineageError):
            spread_from_seeds(graph, ["a"], "downstream")


class EffectiveOwnerTests(unittest.TestCase):
    def test_latest_effective_version_wins(self) -> None:
        graph = lineage(
            [node("pack")], [],
            [owner("pack", 1, "maker", "2026-08-01T00:00:00Z", kind="initial"),
             owner("pack", 2, "fleet", "2026-08-20T00:00:00Z", kind="sale"),
             owner("pack", 3, "lessee", "2026-09-10T00:00:00Z", kind="lease")],
        )
        self.assertEqual(effective_owner(graph, "pack", "2026-08-15T00:00:00Z").holder_id, "maker")
        self.assertEqual(effective_owner(graph, "pack", "2026-09-20T00:00:00Z").holder_id, "lessee")
        self.assertIsNone(effective_owner(graph, "other", "2026-09-20T00:00:00Z"))

    def test_future_versions_are_ignored(self) -> None:
        graph = lineage(
            [node("pack")], [],
            [owner("pack", 1, "maker", "2026-08-01T00:00:00Z", kind="initial"),
             owner("pack", 2, "fleet", "2026-09-30T00:00:00Z")],
        )
        self.assertEqual(effective_owner(graph, "pack", "2026-09-01T00:00:00Z").holder_id, "maker")


class ComputeScopeTests(unittest.TestCase):
    def test_members_holders_and_top_level_capacity(self) -> None:
        graph = lineage(
            [node("lot", kind="cell_lot", capacity="0", top=False),
             node("pack-a", capacity="500"), node("pack-b", capacity="250.5")],
            [edge("e1", "lot", "pack-a", relation="manufactured_from"),
             edge("e2", "lot", "pack-b", relation="manufactured_from")],
            [owner("pack-a", 1, "h1", "2026-08-01T00:00:00Z", kind="initial"),
             owner("pack-b", 1, "h2", "2026-08-01T00:00:00Z", kind="initial")],
        )
        request = ScopeRequest(
            notice_id="n1", seeds=["lot"], direction="downstream",
            as_of="2026-09-20T00:00:00Z", reason="初始", lineage_revision_ids=["r1"],
        )
        result = compute_scope(graph, request)
        self.assertEqual(result["affected_assets"], 3)
        self.assertEqual(result["affected_top_level_capacity_kwh"], "750.500")
        self.assertEqual(result["holder_counts"], {"h1": 1, "h2": 1})
        self.assertEqual([m["asset_id"] for m in result["members"]], ["lot", "pack-a", "pack-b"])

    def test_same_inputs_recompute_identical(self) -> None:
        graph = lineage([node("lot", capacity="0", top=False), node("pack")],
                        [edge("e", "lot", "pack")],
                        [owner("pack", 1, "h", "2026-08-01T00:00:00Z", kind="initial")])
        request = ScopeRequest("n1", ["lot"], "downstream", "2026-09-20T00:00:00Z",
                               "原因", ["r1"])
        first = compute_scope(graph, request)
        second = compute_scope(graph, request)
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(first["output_sha256"], second["output_sha256"])

    def test_base_members_keep_scope_cumulative(self) -> None:
        graph = lineage(
            [node("lot", capacity="0", top=False), node("pack"), node("rack")],
            [edge("e1", "lot", "pack"), edge("e2", "pack", "rack", relation="installed_in")],
            [owner("pack", 1, "h1", "2026-08-01T00:00:00Z", kind="initial"),
             owner("rack", 1, "h2", "2026-08-01T00:00:00Z", kind="initial")],
        )
        # 上游扩散只从 lot 出发，但当前成员 pack/rack 作为根必中，范围不丢成员。
        request = ScopeRequest("n1", ["lot"], "upstream", "2026-09-20T00:00:00Z",
                               "上游证据", ["r2"], base_member_ids=["pack", "rack"])
        result = compute_scope(graph, request)
        self.assertEqual({m["asset_id"] for m in result["members"]}, {"lot", "pack", "rack"})

    def test_member_without_owner_has_null_holder(self) -> None:
        graph = lineage([node("lot", capacity="0", top=False), node("pack")],
                        [edge("e", "lot", "pack")])
        request = ScopeRequest("n1", ["lot"], "downstream", "2026-09-20T00:00:00Z",
                               "原因", ["r1"])
        pack_member = [m for m in compute_scope(graph, request)["members"] if m["asset_id"] == "pack"][0]
        self.assertIsNone(pack_member["holder_id"])


if __name__ == "__main__":
    unittest.main()
