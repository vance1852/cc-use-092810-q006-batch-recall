"""批次召回编排端到端离线验收：覆盖冻结谱系范围、版本化扩散与缩减、
每资产单调措施、所有权转移约束、升级队列、逾期扫描、仪表盘与审计复算。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RecallOrchestrationService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
    service = RecallOrchestrationService(connection, clock)
    for user_id, role in (
        ("lead", "recall_lead"),
        ("field", "field_agent"),
        ("approver", "approver"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 供应商发布电芯批次风险通知，制造商只能找到最初收货记录。
    service.create_notice("lead", {
        "notice_id": "rc-2026-09",
        "supplier_batch_id": "lot-c202",
        "title": "C202 批次内短路风险召回",
        "risk_description": "供应商通知该批次隔膜缺陷可能导致内短路",
        "supplier_ref": "SUP-NTC-2026-018",
        "issued_at": "2026-09-19T00:00:00Z",
        "ack_within_hours": 72,
        "quarantine_within_hours": 168,
        "inspect_within_hours": 336,
        "return_within_hours": 720,
    })

    # 冻结第一版谱系证据：电芯批次→模组→电池包，含转售与租赁的有效所有权版本。
    service.record_lineage_revision("lead", {
        "revision_id": "lineage-1",
        "notice_id": "rc-2026-09",
        "assets": [
            {"asset_id": "lot-c202", "asset_kind": "cell_lot", "capacity_kwh": "0", "top_level": False},
            {"asset_id": "mod-1", "asset_kind": "module", "capacity_kwh": "0", "top_level": False},
            {"asset_id": "mod-2", "asset_kind": "module", "capacity_kwh": "0", "top_level": False},
            {"asset_id": "pack-a", "asset_kind": "battery_pack", "capacity_kwh": "500", "top_level": True},
            {"asset_id": "pack-b", "asset_kind": "battery_pack", "capacity_kwh": "500", "top_level": True},
        ],
        "edges": [
            {"edge_id": "e-lot-m1", "parent_id": "lot-c202", "child_id": "mod-1",
             "relation": "manufactured_from", "effective_from": "2026-08-01T00:00:00Z"},
            {"edge_id": "e-lot-m2", "parent_id": "lot-c202", "child_id": "mod-2",
             "relation": "manufactured_from", "effective_from": "2026-08-01T00:00:00Z"},
            {"edge_id": "e-m1-pa", "parent_id": "mod-1", "child_id": "pack-a",
             "relation": "contained", "effective_from": "2026-08-05T00:00:00Z"},
            {"edge_id": "e-m2-pb", "parent_id": "mod-2", "child_id": "pack-b",
             "relation": "contained", "effective_from": "2026-08-05T00:00:00Z"},
        ],
        "ownership": [
            {"asset_id": "lot-c202", "version": 1, "holder_id": "maker", "kind": "initial",
             "effective_from": "2026-07-28T00:00:00Z"},
            {"asset_id": "mod-1", "version": 1, "holder_id": "maker", "kind": "initial",
             "effective_from": "2026-08-01T00:00:00Z"},
            {"asset_id": "mod-2", "version": 1, "holder_id": "maker", "kind": "initial",
             "effective_from": "2026-08-01T00:00:00Z"},
            {"asset_id": "pack-a", "version": 1, "holder_id": "maker", "kind": "initial",
             "effective_from": "2026-08-05T00:00:00Z"},
            {"asset_id": "pack-a", "version": 2, "holder_id": "fleet-east", "kind": "sale",
             "effective_from": "2026-08-20T00:00:00Z"},
            {"asset_id": "pack-a", "version": 3, "holder_id": "lessee-north", "kind": "lease",
             "effective_from": "2026-09-05T00:00:00Z"},
            {"asset_id": "pack-b", "version": 1, "holder_id": "maker", "kind": "initial",
             "effective_from": "2026-08-05T00:00:00Z"},
            {"asset_id": "pack-b", "version": 2, "holder_id": "operator-x", "kind": "sale",
             "effective_from": "2026-08-22T00:00:00Z"},
            {"asset_id": "pack-b", "version": 3, "holder_id": "operator-y", "kind": "resale",
             "effective_from": "2026-09-12T00:00:00Z"},
        ],
    })

    # 从冻结谱系与有效所有权版本计算初始范围（v1 直接生效）。
    initial = service.compute_initial_scope("lead", "rc-2026-09", {
        "reason": "供应商 C202 批次风险通知：按下游谱系确定初始受影响资产",
    })

    # pack-a（现承租人 lessee-north）独立推进：通知→签收→隔离→现场检查。
    service.record_receipt("field", "rc-2026-09", "pack-a", "notify",
                           "rcpt-pa-n1", holder_id="lessee-north", note="召回通知已送达")
    service.record_receipt("field", "rc-2026-09", "pack-a", "acknowledge",
                           "rcpt-pa-a1", holder_id="lessee-north", note="承租人签收")
    service.record_receipt("field", "rc-2026-09", "pack-a", "quarantine",
                           "rcpt-pa-q1", holder_id="lessee-north", note="已停运隔离")
    service.record_receipt("field", "rc-2026-09", "pack-a", "inspect",
                           "rcpt-pa-i1", holder_id="lessee-north", note="现场检查确认缺陷")

    # pack-b 已转售给 operator-y，通知发出但无人签收。
    service.record_receipt("lead", "rc-2026-09", "pack-b", "notify",
                           "rcpt-pb-n1", holder_id="operator-y", note="通知已发出")

    # 四天后扫描逾期：pack-b 超过签收时限，进入升级队列。
    clock.advance(days=4)
    overdue = service.scan_overdue("lead", "rc-2026-09")
    service.mark_unreachable("field", "rc-2026-09", "pack-b", "电话停机、邮件退回，无法联系 operator-y")

    # 新的谱系证据：pack-a 租约结束返修返还 fleet-east，mod-1 被拆出装入翻新包 pack-c。
    service.record_lineage_revision("lead", {
        "revision_id": "lineage-2",
        "notice_id": "rc-2026-09",
        "previous_revision_id": "lineage-1",
        "assets": [
            {"asset_id": "pack-c", "asset_kind": "battery_pack", "capacity_kwh": "500", "top_level": True},
        ],
        "edges": [
            {"edge_id": "e-pa-m1-remove", "parent_id": "pack-a", "child_id": "mod-1",
             "relation": "removed_from", "effective_from": "2026-09-22T00:00:00Z"},
            {"edge_id": "e-m1-pc", "parent_id": "mod-1", "child_id": "pack-c",
             "relation": "installed_in", "effective_from": "2026-09-22T12:00:00Z"},
        ],
        "ownership": [
            {"asset_id": "pack-a", "version": 4, "holder_id": "fleet-east",
             "kind": "return_from_lease", "effective_from": "2026-09-23T00:00:00Z"},
            {"asset_id": "pack-c", "version": 1, "holder_id": "refurb-shop", "kind": "initial",
             "effective_from": "2026-09-22T12:00:00Z"},
        ],
    })

    # 风险向下游扩散到翻新包：生成可审核的新范围版本提案，由独立批准岗位批准。
    proposal = service.propose_scope_revision("lead", "rc-2026-09", {
        "direction": "downstream",
        "reason": "返修拆分证据：mod-1 被装入翻新包 pack-c，风险随下游扩散",
    })
    service.decide_scope_revision("approver", "rc-2026-09", proposal["version_no"], True,
                                  "谱系证据充分，批准扩散范围")

    # pack-c 的当前持有人 refurb-shop 独立推进措施。
    service.record_receipt("field", "rc-2026-09", "pack-c", "notify",
                           "rcpt-pc-n1", holder_id="refurb-shop", note="翻新车间通知")
    service.record_receipt("field", "rc-2026-09", "pack-c", "acknowledge",
                           "rcpt-pc-a1", holder_id="refurb-shop", note="车间签收")
    service.record_receipt("field", "rc-2026-09", "pack-c", "quarantine",
                           "rcpt-pc-q1", holder_id="refurb-shop", note="翻新包隔离")

    # pack-a 所有权已转移：物理阶段保留（仍停在 inspected），
    # 向新持有人 fleet-east 发出第二轮通知后才能继续返厂。
    service.record_receipt("field", "rc-2026-09", "pack-a", "notify",
                           "rcpt-pa-n2", holder_id="fleet-east", note="返还后向新持有人通知")
    service.record_receipt("field", "rc-2026-09", "pack-a", "acknowledge",
                           "rcpt-pa-a2", holder_id="fleet-east", note="fleet-east 签收")
    service.record_receipt("field", "rc-2026-09", "pack-a", "return_to_factory",
                           "rcpt-pa-r1", holder_id="fleet-east", note="返厂检测")
    service.record_receipt("field", "rc-2026-09", "pack-a", "release",
                           "rcpt-pa-rel", holder_id="fleet-east", note="缺陷消除，解除召回")

    # 供应商补充证据：仅 mod-1 涉事。范围缩减独立提案、独立批准；
    # 已通知 pack-b 的事实保留，不删除不回退。
    shrink = service.propose_shrink("lead", "rc-2026-09", {
        "seeds": ["mod-1"],
        "direction": "downstream",
        "reason": "供应商复核：缺陷仅存在 mod-1 产线，mod-2 不涉事",
    })
    shrink_decision = service.decide_scope_revision(
        "approver", "rc-2026-09", shrink["version_no"], True,
        "供应商证据与检测结果一致，批准缩减",
    )

    tracking_pb = service.asset_tracking("auditor", "rc-2026-09", "pack-b")
    dashboard = service.dashboard("lead", "rc-2026-09")
    history = service.scope_history("auditor", "rc-2026-09")
    recomputed = {
        version_no: service.recompute_scope("auditor", "rc-2026-09", version_no)["matches"]
        for version_no in (1, proposal["version_no"], shrink["version_no"])
    }
    audit = service.audit_chain("auditor")

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "initial_affected_assets": initial["affected_assets"],
        "initial_holders": initial["holder_counts"],
        "scope_versions": len(history["versions"]),
        "scope_version_states": [item["state"] for item in history["versions"]],
        "expansion_added": proposal["change_summary"]["added"],
        "shrink_removed": shrink["change_summary"]["removed"],
        "shrink_approved_by": shrink_decision["shrink_approved_by"],
        "recompute_matches": recomputed,
        "pack_b_preserved": {
            "in_scope": tracking_pb["in_scope"],
            "notified_round": tracking_pb["notified_round"],
            "receipts": len(tracking_pb["receipts"]),
        },
        "pack_a_rounds": service.asset_tracking("auditor", "rc-2026-09", "pack-a")["notified_round"],
        "pack_a_final_stage": service.asset_tracking("auditor", "rc-2026-09", "pack-a")["physical_stage"],
        "overdue_found": [
            item["reason_code"] for item in overdue["open_escalations"] if item["asset_id"] == "pack-b"
        ],
        "open_escalations": sorted(item["reason_code"] for item in dashboard["open_escalations"]),
        "running_top_level_capacity_kwh": dashboard["scope"]["running_top_level_capacity_kwh"],
        "affected_top_level_capacity_kwh": dashboard["scope"]["affected_top_level_capacity_kwh"],
        "stage_counts": dashboard["scope"]["stage_counts"],
        "current_holders": dashboard["scope"]["current_holders"],
        "audit_valid": audit["valid"],
        "audit_events": audit["events"],
        "audit_head_hash": audit["head_hash"],
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行批次召回编排离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
