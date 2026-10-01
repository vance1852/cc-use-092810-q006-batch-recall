from __future__ import annotations

import json
import sqlite3
import unittest

from batch_recall.api import JsonApplication
from batch_recall.service import RecallService


def post(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class RecallApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(RecallService(self.connection))
        coordinator = {"X-Actor-Id": "coord"}
        auditor = {"X-Actor-Id": "audit"}
        self.app.handle("POST", "/users", body=post({"user_id": "coord", "display_name": "协调员", "role": "coordinator"}))
        self.app.handle("POST", "/users", body=post({"user_id": "audit", "display_name": "审计", "role": "auditor"}))
        self.app.handle("POST", "/assets", headers=coordinator, body=post(
            {"asset_id": "lot-1", "asset_kind": "cell_lot", "capacity_kwh": "0"}))
        self.app.handle("POST", "/assets", headers=coordinator, body=post(
            {"asset_id": "pack-1", "asset_kind": "battery_pack", "capacity_kwh": "100"}))
        self.app.handle("POST", "/genealogy/edges", headers=coordinator, body=post({"edges": [{
            "edge_id": "e1", "parent_id": "lot-1", "child_id": "pack-1", "edge_kind": "installed",
            "observed_at": "2026-08-05T08:00:00Z", "evidence_sha256": "1" * 64, "note": ""}]}))
        self.app.handle("POST", "/ownership/versions", headers=coordinator, body=post({"versions": [{
            "asset_id": "pack-1", "version": 1, "holder_id": "operator-east", "mode": "held",
            "effective_at": "2026-08-01T00:00:00Z", "contact_channel": "mailto:e@example.test"}]}))
        self.app.handle("POST", "/notices", headers=coordinator, body=post({
            "notice_id": "n1", "supplier_id": "s1", "component_lot_id": "lot-1",
            "title": "风险", "risk_summary": "隔膜缺陷", "issued_at": "2026-09-19T00:00:00Z",
            "evidence_sha256": "a" * 64}))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_initiate_and_dashboard_route(self) -> None:
        response = self.app.handle("POST", "/recalls", headers={"X-Actor-Id": "coord"},
                                   body=post({"recall_id": "r1", "notice_id": "n1", "sla_hours": 72}))
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["scope_version"], 1)
        dashboard = self.app.handle("GET", "/recalls/r1/dashboard", headers={"X-Actor-Id": "coord"})
        self.assertEqual(dashboard.status, 200)
        self.assertEqual(dashboard.body["scope"]["included_assets"], 2)
        self.assertEqual(dashboard.body["capacity_kwh"]["affected"], "100.000")

    def test_recompute_route(self) -> None:
        self.app.handle("POST", "/recalls", headers={"X-Actor-Id": "coord"},
                        body=post({"recall_id": "r1", "notice_id": "n1"}))
        response = self.app.handle("POST", "/recalls/r1/scopes/1/recompute",
                                   headers={"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["content_matches"])

    def test_requires_actor_header(self) -> None:
        response = self.app.handle("GET", "/recalls/r1/dashboard")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
