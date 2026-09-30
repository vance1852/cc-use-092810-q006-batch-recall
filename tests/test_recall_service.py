from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from recall_orchestration.clock import FrozenClock
from recall_orchestration.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    ValidationFailed,
)
from recall_orchestration.service import RecallOrchestrationService


def base_lineage() -> dict:
    return {
        "revision_id": "r1",
        "notice_id": "n1",
        "assets": [
            {"asset_id": "lot", "asset_kind": "cell_lot", "capacity_kwh": "0", "top_level": False},
            {"asset_id": "pack-a", "asset_kind": "pack", "capacity_kwh": "500", "top_level": True},
            {"asset_id": "pack-b", "asset_kind": "pack", "capacity_kwh": "300", "top_level": True},
        ],
        "edges": [
            {"edge_id": "e1", "parent_id": "lot", "child_id": "pack-a",
             "relation": "manufactured_from", "effective_from": "2026-08-01T00:00:00Z"},
            {"edge_id": "e2", "parent_id": "lot", "child_id": "pack-b",
             "relation": "manufactured_from", "effective_from": "2026-08-02T00:00:00Z"},
        ],
        "ownership": [
            {"asset_id": "lot", "version": 1, "holder_id": "maker", "kind": "initial",
             "effective_from": "2026-07-30T00:00:00Z"},
            {"asset_id": "pack-a", "version": 1, "holder_id": "maker", "kind": "initial",
             "effective_from": "2026-08-01T00:00:00Z"},
            {"asset_id": "pack-a", "version": 2, "holder_id": "ha", "kind": "sale",
             "effective_from": "2026-08-20T00:00:00Z"},
            {"asset_id": "pack-b", "version": 1, "holder_id": "hb", "kind": "initial",
             "effective_from": "2026-08-02T00:00:00Z"},
        ],
    }


class RecallServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.service = RecallOrchestrationService(self.connection, self.clock)
        for user_id, role in (
            ("lead", "recall_lead"),
            ("field", "field_agent"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_notice("lead", {
            "notice_id": "n1", "supplier_batch_id": "lot", "title": "批次风险",
            "risk_description": "内短路", "supplier_ref": "SUP-1",
            "issued_at": "2026-09-19T00:00:00Z",
        })
        self.service.record_lineage_revision("lead", base_lineage())

    def tearDown(self) -> None:
        self.connection.close()

    def initial_scope(self):
        return self.service.compute_initial_scope("lead", "n1", {"reason": "初始范围"})

    # ── 初始范围与所有权 ──────────────────────────────────────

    def test_initial_scope_from_frozen_lineage(self) -> None:
        scope = self.initial_scope()
        self.assertEqual(scope["version_no"], 1)
        self.assertEqual(scope["state"], "active")
        self.assertEqual(scope["affected_assets"], 3)
        self.assertEqual(scope["affected_top_level_capacity_kwh"], "800.000")
        holders = {m["asset_id"]: m["holder_id"] for m in scope["members"]}
        self.assertEqual(holders, {"lot": "maker", "pack-a": "ha", "pack-b": "hb"})

    def test_initial_scope_takes_effective_owner_at_as_of(self) -> None:
        scope = self.service.compute_initial_scope(
            "lead", "n1", {"reason": "历史切面", "as_of": "2026-08-10T00:00:00Z"}
        )
        holders = {m["asset_id"]: m["holder_id"] for m in scope["members"]}
        self.assertEqual(holders["pack-a"], "maker")  # 8 月 20 日的转售尚未生效

    def test_initial_scope_can_only_be_computed_once(self) -> None:
        self.initial_scope()
        with self.assertRaises(InvalidState):
            self.initial_scope()

    def test_frozen_revision_rejects_duplicate_content(self) -> None:
        duplicate = dict(base_lineage(), revision_id="r1-copy")
        with self.assertRaises(Conflict):
            self.service.record_lineage_revision("lead", duplicate)

    def test_ownership_versions_must_be_contiguous(self) -> None:
        with self.assertRaises(Conflict):
            self.service.record_lineage_revision("lead", {
                "revision_id": "rx", "previous_revision_id": "r1",
                "ownership": [{"asset_id": "pack-a", "version": 5, "holder_id": "hx",
                               "kind": "resale", "effective_from": "2026-09-18T00:00:00Z"}],
            })

    # ── 每资产措施：单调、幂等、拒绝乱序 ───────────────────────

    def drive_pack_a(self) -> None:
        self.service.record_receipt("field", "n1", "pack-a", "notify", "k-n", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "acknowledge", "k-a", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "quarantine", "k-q", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "inspect", "k-i", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "return_to_factory", "k-r", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "release", "k-rel", holder_id="ha")

    def test_full_physical_flow_per_asset(self) -> None:
        self.initial_scope()
        self.drive_pack_a()
        tracking = self.service.asset_tracking("auditor", "n1", "pack-a")
        self.assertEqual(tracking["physical_stage"], "released")
        self.assertEqual(len(tracking["receipts"]), 6)

    def test_duplicate_receipts_do_not_regress_or_double_count(self) -> None:
        self.initial_scope()
        self.service.record_receipt("field", "n1", "pack-a", "notify", "k-n", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "acknowledge", "k-a", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "quarantine", "k-q", holder_id="ha")
        # 不同幂等键的重复回执：回放既有状态，不新增回执行。
        replay = self.service.record_receipt(
            "field", "n1", "pack-a", "quarantine", "k-q-dup", holder_id="ha"
        )
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["physical_stage"], "quarantined")
        # 同一幂等键重放：返回首次结果。
        same_key = self.service.record_receipt(
            "field", "n1", "pack-a", "notify", "k-n", holder_id="ha"
        )
        self.assertTrue(same_key.get("idempotent_replay"))
        receipts = self.connection.execute(
            "SELECT count(*) AS n FROM action_receipts WHERE asset_id='pack-a'"
        ).fetchone()["n"]
        self.assertEqual(receipts, 3)

    def test_out_of_order_and_skip_steps_rejected(self) -> None:
        self.initial_scope()
        with self.assertRaises(InvalidState):  # 未通知先签收
            self.service.record_receipt("field", "n1", "pack-a", "quarantine", "x", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "notify", "k-n", holder_id="ha")
        with self.assertRaises(InvalidState):  # 跳过签收/隔离直接检查
            self.service.record_receipt("field", "n1", "pack-a", "inspect", "x", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "acknowledge", "k-a", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "quarantine", "k-q", holder_id="ha")
        # 重复隔离回执被幂等吸收：不倒退、不新增。
        duplicate = self.service.record_receipt(
            "field", "n1", "pack-a", "quarantine", "k-q2", holder_id="ha"
        )
        self.assertTrue(duplicate["duplicate"])
        self.service.record_receipt("field", "n1", "pack-a", "inspect", "k-i", holder_id="ha")
        with self.assertRaises(InvalidState):  # 已检查不能倒退登记隔离
            self.service.record_receipt("field", "n1", "pack-a", "quarantine", "x3", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "return_to_factory", "k-rf", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "release", "k-rel", holder_id="ha")
        with self.assertRaises(InvalidState):  # 已解除不得倒退
            self.service.record_receipt("field", "n1", "pack-a", "quarantine", "x4", holder_id="ha")

    def test_receipt_holder_must_match_current_owner(self) -> None:
        self.initial_scope()
        with self.assertRaises(Conflict):
            self.service.record_receipt("field", "n1", "pack-a", "notify", "k", holder_id="intruder")

    def test_same_idempotency_key_with_different_payload_conflicts(self) -> None:
        self.initial_scope()
        self.service.record_receipt("field", "n1", "pack-a", "notify", "shared-key", holder_id="ha")
        with self.assertRaises(Conflict):
            self.service.record_receipt("field", "n1", "pack-a", "acknowledge", "shared-key", holder_id="ha")

    def test_actions_on_unknown_or_shrunk_asset_rejected(self) -> None:
        with self.assertRaises(NotFound):
            self.service.record_receipt("field", "n1", "ghost", "notify", "k")
        self.initial_scope()
        shrink = self.service.propose_shrink("lead", "n1", {"seeds": ["pack-a"], "reason": "缩减"})
        self.service.decide_scope_revision("approver", "n1", shrink["version_no"], True, "ok")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("field", "n1", "pack-b", "notify", "k", holder_id="hb")

    # ── 所有权转移：约束保留、新轮通知 ─────────────────────────

    def test_recall_constraint_follows_ownership_transfer(self) -> None:
        self.initial_scope()
        self.service.record_receipt("field", "n1", "pack-a", "notify", "k-n", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "acknowledge", "k-a", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "quarantine", "k-q", holder_id="ha")
        self.service.record_lineage_revision("lead", {
            "revision_id": "r2", "previous_revision_id": "r1",
            "ownership": [{"asset_id": "pack-a", "version": 3, "holder_id": "hbuyer",
                           "kind": "resale", "effective_from": "2026-09-21T00:00:00Z"}],
        })
        tracking = self.service.asset_tracking("auditor", "n1", "pack-a")
        self.assertEqual(tracking["current_holder_id"], "hbuyer")
        self.assertEqual(tracking["physical_stage"], "quarantined")  # 物理阶段不回退
        # 转移后自动进入待通知升级队列。
        dashboard = self.service.dashboard("lead", "n1")
        self.assertIn("transfer_pending_notify",
                      [e["reason_code"] for e in dashboard["open_escalations"]])
        # 旧持有人无法继续推进（持有人不一致或要求新一轮通知，均被拒绝）。
        with self.assertRaises((Conflict, InvalidState)):
            self.service.record_receipt("field", "n1", "pack-a", "inspect", "k-i", holder_id="ha")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("field", "n1", "pack-a", "inspect", "k-i0")
        # 向新持有人发出第二轮通知后可继续，升级自动解除。
        notify2 = self.service.record_receipt(
            "field", "n1", "pack-a", "notify", "k-n2", holder_id="hbuyer"
        )
        self.assertEqual(notify2["round_no"], 2)
        self.service.record_receipt("field", "n1", "pack-a", "acknowledge", "k-a2", holder_id="hbuyer")
        self.service.record_receipt("field", "n1", "pack-a", "inspect", "k-i2", holder_id="hbuyer")
        dashboard = self.service.dashboard("lead", "n1")
        self.assertNotIn("transfer_pending_notify",
                         [e["reason_code"] for e in dashboard["open_escalations"]])

    # ── 范围版本：扩散、缩减、独立批准 ─────────────────────────

    def test_expansion_proposal_requires_approval_and_keeps_history(self) -> None:
        self.initial_scope()
        self.service.record_lineage_revision("lead", {
            "revision_id": "r2", "previous_revision_id": "r1",
            "assets": [{"asset_id": "rack-1", "asset_kind": "rack",
                        "capacity_kwh": "500", "top_level": True}],
            "edges": [{"edge_id": "e3", "parent_id": "pack-a", "child_id": "rack-1",
                       "relation": "installed_in", "effective_from": "2026-09-18T00:00:00Z"}],
            "ownership": [{"asset_id": "rack-1", "version": 1, "holder_id": "hr",
                           "kind": "initial", "effective_from": "2026-09-18T00:00:00Z"}],
        })
        proposal = self.service.propose_scope_revision(
            "lead", "n1", {"direction": "downstream", "reason": "现场装入新证据"}
        )
        self.assertEqual(proposal["state"], "proposed")
        self.assertEqual(proposal["change_summary"]["added"], ["rack-1"])
        # 有待决提案时不能再提。
        with self.assertRaises(InvalidState):
            self.service.propose_scope_revision("lead", "n1", {"reason": "重复提案"})
        # 现场/负责人都不能批准。
        with self.assertRaises(Forbidden):
            self.service.decide_scope_revision("lead", "n1", proposal["version_no"], True, "x")
        approved = self.service.decide_scope_revision(
            "approver", "n1", proposal["version_no"], True, "证据充分"
        )
        self.assertEqual(approved["state"], "active")
        history = self.service.scope_history("auditor", "n1")["versions"]
        self.assertEqual([v["state"] for v in history], ["superseded", "active"])
        self.assertEqual(history[1]["reason"], "现场装入新证据")

    def test_rejected_expansion_leaves_active_scope_untouched(self) -> None:
        self.initial_scope()
        self.service.record_lineage_revision("lead", {
            "revision_id": "r2", "previous_revision_id": "r1",
            "assets": [{"asset_id": "rack-1", "asset_kind": "rack",
                        "capacity_kwh": "500", "top_level": True}],
            "edges": [{"edge_id": "e3", "parent_id": "pack-a", "child_id": "rack-1",
                       "relation": "installed_in", "effective_from": "2026-09-18T00:00:00Z"}],
            "ownership": [{"asset_id": "rack-1", "version": 1, "holder_id": "hr",
                           "kind": "initial", "effective_from": "2026-09-18T00:00:00Z"}],
        })
        proposal = self.service.propose_scope_revision("lead", "n1", {"reason": "扩散?"})
        decision = self.service.decide_scope_revision(
            "approver", "n1", proposal["version_no"], False, "证据不足"
        )
        self.assertEqual(decision["state"], "rejected")
        self.assertEqual(self.service.dashboard("lead", "n1")["scope"]["assets_in_scope"], 3)
        # 拒绝后可以重新提案。
        second = self.service.propose_scope_revision("lead", "n1", {"reason": "补充证据"})
        self.assertEqual(second["version_no"], 3)

    def test_expansion_without_new_assets_rejected(self) -> None:
        self.initial_scope()
        with self.assertRaises(InvalidState):
            self.service.propose_scope_revision("lead", "n1", {"reason": "没有新证据"})

    def test_shrink_requires_independent_approval_and_preserves_notified_facts(self) -> None:
        self.initial_scope()
        self.service.record_receipt("field", "n1", "pack-b", "notify", "k-n", holder_id="hb")
        shrink = self.service.propose_shrink("lead", "n1", {"seeds": ["pack-a"], "reason": "仅 pack-a"})
        self.assertEqual(shrink["change_summary"]["removed"], ["lot", "pack-b"])
        with self.assertRaises(Forbidden):
            self.service.decide_scope_revision("field", "n1", shrink["version_no"], True, "x")
        approved = self.service.decide_scope_revision(
            "approver", "n1", shrink["version_no"], True, "批准"
        )
        self.assertEqual(approved["shrink_approved_by"], "approver")
        # 已通知事实保留：行不删除、阶段/轮次不回退，仅退出当前范围。
        tracking = self.service.asset_tracking("auditor", "n1", "pack-b")
        self.assertFalse(tracking["in_scope"])
        self.assertEqual(tracking["notified_round"], 1)
        self.assertEqual(len(tracking["receipts"]), 1)
        history = self.service.scope_history("auditor", "n1")["versions"]
        self.assertEqual(len(history), 2)  # 缩减不抹去历史版本

    def test_shrink_without_removal_rejected(self) -> None:
        self.initial_scope()
        with self.assertRaises(InvalidState):
            self.service.propose_shrink("lead", "n1", {"seeds": ["lot"], "reason": "没有收窄"})

    def test_every_scope_version_recomputes(self) -> None:
        self.initial_scope()
        self.service.record_lineage_revision("lead", {
            "revision_id": "r2", "previous_revision_id": "r1",
            "assets": [{"asset_id": "rack-1", "asset_kind": "rack",
                        "capacity_kwh": "500", "top_level": True}],
            "edges": [{"edge_id": "e3", "parent_id": "pack-a", "child_id": "rack-1",
                       "relation": "installed_in", "effective_from": "2026-09-18T00:00:00Z"}],
            "ownership": [{"asset_id": "rack-1", "version": 1, "holder_id": "hr",
                           "kind": "initial", "effective_from": "2026-09-18T00:00:00Z"}],
        })
        proposal = self.service.propose_scope_revision("lead", "n1", {"reason": "扩散"})
        self.service.decide_scope_revision("approver", "n1", proposal["version_no"], True, "ok")
        shrink = self.service.propose_shrink("lead", "n1", {"seeds": ["rack-1"], "reason": "缩减"})
        self.service.decide_scope_revision("approver", "n1", shrink["version_no"], True, "ok")
        for version_no in (1, proposal["version_no"], shrink["version_no"]):
            self.assertTrue(
                self.service.recompute_scope("auditor", "n1", version_no)["matches"],
                f"版本 {version_no} 复算不一致",
            )

    # ── 升级队列与逾期 ────────────────────────────────────────

    def test_unreachable_enters_and_resolves_escalation_queue(self) -> None:
        self.initial_scope()
        opened = self.service.mark_unreachable("field", "n1", "pack-a", "联系不上")
        self.assertEqual(opened["state"], "open")
        duplicate = self.service.mark_unreachable("field", "n1", "pack-a", "再次失联")
        self.assertTrue(duplicate.get("already_open"))
        dashboard = self.service.dashboard("lead", "n1")
        self.assertEqual(len(dashboard["open_escalations"]), 1)
        self.service.resolve_escalation("field", opened["escalation_id"], "已取得联系")
        self.assertEqual(self.service.dashboard("lead", "n1")["overdue_count"], 0)

    def test_overdue_scan_uses_notice_slas(self) -> None:
        self.service.create_notice("lead", {
            "notice_id": "n2", "supplier_batch_id": "lot", "title": "t",
            "risk_description": "d", "supplier_ref": "s2",
            "ack_within_hours": 24,
        })
        self.service.compute_initial_scope("lead", "n2", {"reason": "x"})
        self.service.record_receipt("lead", "n2", "pack-a", "notify", "k", holder_id="ha")
        self.clock.advance(hours=25)
        scan = self.service.scan_overdue("lead", "n2")
        self.assertEqual(
            [e["reason_code"] for e in scan["open_escalations"] if e["asset_id"] == "pack-a"],
            ["no_acknowledgement"],
        )
        # 签收后再次扫描不再报告同一逾期。
        self.service.record_receipt("field", "n2", "pack-a", "acknowledge", "k2", holder_id="ha")
        scan2 = self.service.scan_overdue("field", "n2")
        self.assertFalse(
            [e for e in scan2["open_escalations"]
             if e["asset_id"] == "pack-a" and e["reason_code"] == "no_acknowledgement"]
        )

    def test_unknown_holder_is_unreachable(self) -> None:
        # pack-c 在范围内但没有所有权版本。
        self.service.record_lineage_revision("lead", {
            "revision_id": "r2", "previous_revision_id": "r1",
            "assets": [{"asset_id": "pack-c", "asset_kind": "pack",
                        "capacity_kwh": "100", "top_level": True}],
            "edges": [{"edge_id": "e3", "parent_id": "lot", "child_id": "pack-c",
                       "relation": "manufactured_from", "effective_from": "2026-09-01T00:00:00Z"}],
        })
        self.initial_scope()
        scan = self.service.scan_overdue("lead", "n1")
        self.assertIn("unreachable",
                      [e["reason_code"] for e in scan["open_escalations"] if e["asset_id"] == "pack-c"])

    # ── 仪表盘与审计 ──────────────────────────────────────────

    def test_dashboard_shows_responsibility_and_running_capacity(self) -> None:
        self.initial_scope()
        self.service.record_receipt("field", "n1", "pack-a", "notify", "n", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "acknowledge", "a", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "quarantine", "q", holder_id="ha")
        dashboard = self.service.dashboard("lead", "n1")
        self.assertEqual(dashboard["current_version"]["version_no"], 1)
        self.assertEqual(dashboard["scope"]["affected_top_level_capacity_kwh"], "800.000")
        # pack-a 隔离中仍在运行现场（计入在运容量），pack-b 待通知也计入。
        self.assertEqual(dashboard["scope"]["running_top_level_capacity_kwh"], "800.000")
        self.assertEqual(dashboard["scope"]["stage_counts"]["quarantined"], 1)
        self.assertEqual(dashboard["scope"]["current_holders"]["ha"], 1)
        # pack-a 返厂后离开运行现场。
        self.service.record_receipt("field", "n1", "pack-a", "inspect", "i", holder_id="ha")
        self.service.record_receipt("field", "n1", "pack-a", "return_to_factory", "r", holder_id="ha")
        dashboard = self.service.dashboard("lead", "n1")
        self.assertEqual(dashboard["scope"]["running_top_level_capacity_kwh"], "300.000")

    def test_audit_chain_is_valid(self) -> None:
        self.initial_scope()
        self.service.record_receipt("field", "n1", "pack-a", "notify", "n", holder_id="ha")
        chain = self.service.audit_chain("auditor")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_notice("field", {
                "notice_id": "x", "supplier_batch_id": "lot", "title": "t",
                "risk_description": "d", "supplier_ref": "s",
            })
        with self.assertRaises(Forbidden):
            self.service.record_lineage_revision("approver", {
                "revision_id": "rz", "assets": [], "edges": [], "ownership": [],
            })
        # 现场可以看仪表盘，但 auditor 只读，不能发通知也不能登记措施。
        self.initial_scope()
        self.assertEqual(self.service.dashboard("field", "n1")["notice_id"], "n1")
        with self.assertRaises(Forbidden):
            self.service.record_receipt("auditor", "n1", "pack-a", "notify", "k")
        # 负责人不能批准自己的范围提案。
        self.service.record_lineage_revision("lead", {
            "revision_id": "r2", "previous_revision_id": "r1",
            "assets": [{"asset_id": "rack-1", "asset_kind": "rack",
                        "capacity_kwh": "100", "top_level": True}],
            "edges": [{"edge_id": "e3", "parent_id": "pack-a", "child_id": "rack-1",
                       "relation": "installed_in", "effective_from": "2026-09-18T00:00:00Z"}],
            "ownership": [{"asset_id": "rack-1", "version": 1, "holder_id": "hr",
                           "kind": "initial", "effective_from": "2026-09-18T00:00:00Z"}],
        })
        proposal = self.service.propose_scope_revision("lead", "n1", {"reason": "扩散"})
        with self.assertRaises(Forbidden):
            self.service.decide_scope_revision("lead", "n1", proposal["version_no"], True, "x")

    def test_input_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_notice("lead", {
                "notice_id": "n9", "supplier_batch_id": "lot", "title": "t",
                "risk_description": "d", "supplier_ref": "s", "ack_within_hours": 0,
            })
        with self.assertRaises(ValidationFailed):
            self.service.record_lineage_revision("lead", {"revision_id": "rz"})


if __name__ == "__main__":
    unittest.main()
