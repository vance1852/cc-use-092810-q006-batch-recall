from __future__ import annotations

import json
import sqlite3
import unittest

from recall_orchestration.api import JsonApplication
from recall_orchestration.service import RecallOrchestrationService


class RecallApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(RecallOrchestrationService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def request(self, method: str, path: str, payload=None, headers=None, status: int = 200):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = self.app.handle(method, path, headers or {}, body)
        self.assertEqual(response.status, status, response.body)
        return response.body

    def test_health(self) -> None:
        body = self.request("GET", "/health")
        self.assertEqual(body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_end_to_end_routes(self) -> None:
        self.request("POST", "/users",
                     {"user_id": "lead", "display_name": "负责人", "role": "recall_lead"}, status=201)
        self.request("POST", "/users",
                     {"user_id": "field", "display_name": "现场", "role": "field_agent"}, status=201)
        self.request("POST", "/users",
                     {"user_id": "appr", "display_name": "批准", "role": "approver"}, status=201)
        self.request("POST", "/users",
                     {"user_id": "aud", "display_name": "审计", "role": "auditor"}, status=201)
        lead = {"x-actor-id": "lead"}
        field = {"x-actor-id": "field"}
        appr = {"x-actor-id": "appr"}
        aud = {"x-actor-id": "aud"}

        self.request("POST", "/notices", {
            "notice_id": "n1", "supplier_batch_id": "lot", "title": "t",
            "risk_description": "d", "supplier_ref": "s",
        }, headers=lead, status=201)
        self.request("POST", "/lineage/revisions", {
            "revision_id": "r1",
            "assets": [
                {"asset_id": "lot", "asset_kind": "cell_lot", "capacity_kwh": "0", "top_level": False},
                {"asset_id": "pack-1", "asset_kind": "pack", "capacity_kwh": "100", "top_level": True},
            ],
            "edges": [{"edge_id": "e1", "parent_id": "lot", "child_id": "pack-1",
                       "relation": "manufactured_from", "effective_from": "2026-09-01T00:00:00Z"}],
            "ownership": [
                {"asset_id": "lot", "version": 1, "holder_id": "maker", "kind": "initial",
                 "effective_from": "2026-08-30T00:00:00Z"},
                {"asset_id": "pack-1", "version": 1, "holder_id": "h1", "kind": "initial",
                 "effective_from": "2026-09-01T00:00:00Z"},
            ],
        }, headers=lead, status=201)
        self.request("POST", "/notices/n1/scope/initial", {"reason": "初始"}, headers=lead, status=201)
        self.request("POST", "/notices/n1/assets/pack-1/receipts", {
            "action": "notify", "idempotency_key": "k1", "holder_id": "h1",
        }, headers=field, status=201)
        self.request("POST", "/notices/n1/assets/pack-1/receipts", {
            "action": "acknowledge", "idempotency_key": "k2", "holder_id": "h1",
        }, headers=field, status=201)
        tracking = self.request("GET", "/notices/n1/assets/pack-1/tracking", headers=field)
        self.assertEqual(tracking["acknowledged_round"], 1)

        shrink = self.request("POST", "/notices/n1/scope/shrinks",
                              {"seeds": ["pack-1"], "reason": "收窄"}, headers=lead, status=201)
        decision = self.request(
            "POST", f"/notices/n1/scope/versions/{shrink['version_no']}/decision",
            {"approve": True, "note": "批准"}, headers=appr,
        )
        self.assertEqual(decision["state"], "active")

        history = self.request("GET", "/notices/n1/scope/history", headers=lead)
        self.assertEqual(len(history["versions"]), 2)
        recomputed = self.request(
            "POST", f"/notices/n1/scope/versions/{shrink['version_no']}/recompute", headers=aud
        )
        self.assertTrue(recomputed["matches"])
        dashboard = self.request("GET", "/notices/n1/dashboard", headers=lead)
        self.assertEqual(dashboard["scope"]["assets_in_scope"], 1)
        chain = self.request("GET", "/audit/chain", headers=aud)
        self.assertTrue(chain["valid"])

    def test_missing_actor_header_rejected(self) -> None:
        response = self.app.handle(
            "POST", "/notices",
            body=json.dumps({"notice_id": "n", "supplier_batch_id": "l", "title": "t",
                             "risk_description": "d", "supplier_ref": "s"}).encode(),
        )
        self.assertEqual(response.status, 422)

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"x-actor-id": "lead"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
