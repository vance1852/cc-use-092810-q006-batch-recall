from __future__ import annotations

import unittest
from pathlib import Path

from batch_recall.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class RecallAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["initial_scope_version"], 1)
        self.assertEqual(result["initial_asset_count"], 5)
        self.assertEqual(result["ack_duplicate_state"], "duplicate")
        self.assertEqual(result["out_of_order_state"], "out_of_order")
        self.assertEqual(result["out_of_order_stage"], "inspect")
        self.assertEqual(result["quarantine_in_transfer_mode"], "in_transfer")
        self.assertEqual(result["expanded_version"], 2)
        self.assertIn("pack-3", result["new_assets"])
        self.assertEqual(result["pack1_final_stage"], "closed")
        self.assertTrue(result["self_approval_blocked"])
        self.assertEqual(result["reduction_status"], "effective")
        self.assertTrue(result["notice_history_preserved_after_reduction"])
        self.assertTrue(result["reduction_stops_actions"])
        self.assertGreaterEqual(result["overdue_opened"], 1)
        self.assertEqual(result["running_affected_capacity_kwh"], "800.000")
        self.assertTrue(result["recompute_v1_matches"])
        self.assertTrue(result["recompute_v2_matches"])
        self.assertTrue(result["recompute_v3_matches"])
        self.assertTrue(result["audit_chain"]["valid"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["schema"]["schema_version"], "1")
        self.assertEqual(len(result["version_reasons"]), 3)


if __name__ == "__main__":
    unittest.main()
