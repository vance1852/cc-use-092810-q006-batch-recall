from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from batch_recall.clock import FrozenClock
from batch_recall.errors import Forbidden, InvalidState
from batch_recall.service import RecallService
from batch_recall.storage import inspect_schema


def edge(edge_id: str, parent: str, child: str, kind: str = "assembly", when: str = "2026-08-02T08:00:00Z") -> dict:
    return {"edge_id": edge_id, "parent_id": parent, "child_id": child, "edge_kind": kind,
            "observed_at": when, "evidence_sha256": "1" * 64, "note": ""}


def owner(asset_id: str, version: int, holder: str, mode: str = "held", when: str = "2026-08-01T00:00:00Z") -> dict:
    return {"asset_id": asset_id, "version": version, "holder_id": holder, "mode": mode,
            "effective_at": when, "contact_channel": f"mailto:{holder}@example.test"}


class RecallServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.service = RecallService(self.connection, self.clock)
        for user_id, role in (("coord", "coordinator"), ("boss", "approver"),
                              ("field-1", "field"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        for asset_id, kind, capacity in (
            ("lot-1", "cell_lot", "0"), ("cell-1", "cell", "0"),
            ("mod-1", "module", "50"), ("pack-1", "battery_pack", "100"),
            ("pack-2", "battery_pack", "100"), ("orphan-cell", "cell", "0"),
        ):
            self.service.register_asset("coord", asset_id, kind, capacity, True)
        self.service.record_genealogy_edges("coord", [
            edge("e1", "lot-1", "cell-1"),
            edge("e2", "mod-1", "cell-1"),
            edge("e3", "pack-1", "mod-1", "installed"),
            edge("e4", "pack-2", "orphan-cell", "repair"),
            edge("e5", "lot-1", "orphan-cell", "assembly"),
        ])
        # orphan-cell 在谱系内但没有所有权版本，用于验证无法联系升级。
        self.service.record_ownership("coord", [
            owner("lot-1", 1, "manufacturer"),
            owner("cell-1", 1, "manufacturer"),
            owner("mod-1", 1, "manufacturer"),
            owner("pack-1", 1, "operator-east"),
        ])
        self.service.publish_notice("coord", {
            "notice_id": "n1", "supplier_id": "s1", "component_lot_id": "lot-1",
            "title": "批次风险", "risk_summary": "隔膜缺陷", "issued_at": "2026-09-19T00:00:00Z",
            "evidence_sha256": "a" * 64,
        })
        self.service.initiate_recall("coord", "r1", "n1", 72)

    def tearDown(self) -> None:
        self.connection.close()

    def test_initial_scope_closure_and_missing_owner_escalation(self) -> None:
        version = self.service.scope_version("audit", "r1", 1)
        assets = {entry["asset_id"] for entry in version["entries"]}
        self.assertEqual(assets, {"lot-1", "cell-1", "mod-1", "pack-1", "pack-2", "orphan-cell"})
        pack1 = next(e for e in version["entries"] if e["asset_id"] == "pack-1")
        self.assertEqual([step["asset_id"] for step in pack1["match_path"]],
                         ["lot-1", "cell-1", "mod-1", "pack-1"])
        escalations = self.service.list_escalations("coord", "r1", "open")["escalations"]
        self.assertEqual([item["asset_id"] for item in escalations], ["orphan-cell", "pack-2"])
        self.assertTrue(all(item["reason"] == "unreachable" for item in escalations))
        action = self.service.asset_detail("audit", "r1", "orphan-cell")["action"]
        self.assertEqual(action["blocked"], 1)

    def test_duplicate_and_out_of_order_receipts_never_regress(self) -> None:
        first = self.service.record_action("field-1", "r1", "pack-1", "acknowledge", "k1")
        self.assertEqual(first["state"], "done")
        self.assertEqual(first["next_stage"], "quarantine")
        replay = self.service.record_action("field-1", "r1", "pack-1", "acknowledge", "k1")
        self.assertTrue(replay["replayed"])
        duplicate = self.service.record_action("field-1", "r1", "pack-1", "acknowledge", "k2")
        self.assertEqual(duplicate["state"], "duplicate")
        self.assertEqual(duplicate["current_stage"], "quarantine")
        jumped = self.service.record_action("field-1", "r1", "pack-1", "return_to_oem", "k3")
        self.assertEqual(jumped["state"], "out_of_order")
        self.assertEqual(jumped["current_stage"], "quarantine")
        detail = self.service.asset_detail("audit", "r1", "pack-1")
        stages = {row["stage"] for row in detail["completions"]}
        self.assertEqual(stages, {"notify", "acknowledge"})

    def test_ownership_transfer_keeps_recall_constraint(self) -> None:
        self.service.record_action("field-1", "r1", "pack-1", "acknowledge", "k1")
        self.service.record_ownership("coord", [
            owner("pack-1", 2, "operator-east", "in_transfer", "2026-09-21T00:00:00Z"),
        ])
        result = self.service.record_action("field-1", "r1", "pack-1", "quarantine", "k2")
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["holder_id"], "operator-east")
        self.assertEqual(result["owner_mode"], "in_transfer")
        self.service.record_ownership("coord", [
            owner("pack-1", 3, "operator-south", "held", "2026-09-22T00:00:00Z"),
        ])
        detail = self.service.asset_detail("audit", "r1", "pack-1")
        self.assertEqual(detail["action"]["latest_holder_id"], "operator-south")

    def test_reduction_requires_independent_approval_and_keeps_notice_facts(self) -> None:
        reduction = self.service.request_scope_reduction("coord", "r1", ["lot-1"], "复检无风险")
        self.assertEqual(reduction["status"], "pending_approval")
        with self.assertRaises(Forbidden):
            self.service.review_scope_reduction("coord", "r1", reduction["scope_version"], True, "自批")
        # 批准前缩减不影响措施。
        detail = self.service.asset_detail("audit", "r1", "lot-1")
        self.assertEqual(detail["action"]["in_scope"], 1)
        approved = self.service.review_scope_reduction("boss", "r1", reduction["scope_version"], True, "同意")
        self.assertEqual(approved["status"], "effective")
        detail = self.service.asset_detail("audit", "r1", "lot-1")
        self.assertEqual(detail["action"]["in_scope"], 0)
        self.assertTrue(any(row["stage"] == "notify" and row["state"] == "done"
                            for row in detail["receipts"]))
        with self.assertRaises(InvalidState):
            self.service.record_action("field-1", "r1", "lot-1", "acknowledge", "k9")

    def test_expand_after_new_genealogy_is_recomputable(self) -> None:
        self.service.register_asset("coord", "cell-2", "cell", "0")
        self.service.register_asset("coord", "pack-3", "battery_pack", "100")
        self.service.record_genealogy_edges("coord", [
            edge("e6", "lot-1", "cell-2", "assembly", "2026-08-03T00:00:00Z"),
            edge("e7", "pack-3", "cell-2", "split", "2026-09-01T00:00:00Z"),
        ])
        self.service.record_ownership("coord", [owner("cell-2", 1, "manufacturer"),
                                                owner("pack-3", 1, "lessee-y")])
        expanded = self.service.expand_scope("coord", "r1", "返修拆分扩散")
        self.assertEqual(expanded["scope_version"], 2)
        self.assertIn("pack-3", expanded["new_assets"])
        with self.assertRaises(InvalidState):
            self.service.expand_scope("coord", "r1", "没有新证据")
        for version in (1, 2):
            recomputed = self.service.recompute_scope("audit", "r1", version)
            self.assertTrue(recomputed["content_matches"])

    def test_overdue_sweep_opens_escalation(self) -> None:
        self.clock.advance(hours=100)
        result = self.service.sweep_overdue("coord", "r1")
        # lot-1、cell-1、mod-1、pack-1 均停在签收阶段；orphan-cell 已有未联系升级。
        self.assertEqual(result["opened"], 4)
        dashboard = self.service.dashboard("coord", "r1")
        self.assertGreaterEqual(len(dashboard["overdue_actions"]), 4)
        self.assertEqual(dashboard["capacity_kwh"]["affected"], "250.000")
        self.assertEqual(dashboard["capacity_kwh"]["running_affected"], "250.000")

    def test_audit_chain_is_valid(self) -> None:
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)
        self.assertEqual(len(chain["head_hash"]), 64)

    def test_schema_contains_required_tables(self) -> None:
        schema = inspect_schema(self.connection)
        self.assertEqual(schema["missing_tables"], [])
        self.assertEqual(schema["schema_version"], "1")


if __name__ == "__main__":
    unittest.main()
