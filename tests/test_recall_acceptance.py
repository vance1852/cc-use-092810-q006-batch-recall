from __future__ import annotations

import unittest
from pathlib import Path

from recall_orchestration.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class RecallAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["initial_affected_assets"], 5)
        self.assertEqual(result["scope_versions"], 3)
        self.assertEqual(result["scope_version_states"], ["superseded", "superseded", "active"])
        self.assertEqual(result["expansion_added"], ["pack-c"])
        self.assertIn("pack-b", result["shrink_removed"])
        self.assertEqual(result["shrink_approved_by"], "approver")
        # 缩减不抹除已通知事实。
        self.assertFalse(result["pack_b_preserved"]["in_scope"])
        self.assertEqual(result["pack_b_preserved"]["notified_round"], 1)
        # 所有权转移经历两轮通知，物理阶段最终解除。
        self.assertEqual(result["pack_a_rounds"], 2)
        self.assertEqual(result["pack_a_final_stage"], "released")
        # 无法联系与逾期进入升级队列。
        self.assertIn("unreachable", result["open_escalations"])
        self.assertIn("no_acknowledgement", result["overdue_found"])
        # 在运容量随返厂/解除下降，全部版本可复算，审计链有效。
        self.assertEqual(result["running_top_level_capacity_kwh"], "500.000")
        self.assertTrue(all(result["recompute_matches"].values()))
        self.assertTrue(result["audit_valid"])
        self.assertGreater(result["audit_events"], 0)


if __name__ == "__main__":
    unittest.main()
