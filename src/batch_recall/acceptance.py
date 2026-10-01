"""批次召回编排的离线验收：覆盖通知、扩散、转移、缩减、升级与复算全流程。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RecallService
from .storage import inspect_schema


def _edge(edge_id: str, parent: str, child: str, kind: str, observed_at: str) -> dict:
    return {
        "edge_id": edge_id,
        "parent_id": parent,
        "child_id": child,
        "edge_kind": kind,
        "observed_at": observed_at,
        "evidence_sha256": "1" * 64,
        "note": "",
    }


def _owner(asset_id: str, version: int, holder: str, mode: str = "held") -> dict:
    return {
        "asset_id": asset_id,
        "version": version,
        "holder_id": holder,
        "mode": mode,
        "effective_at": "2026-08-01T00:00:00Z",
        "contact_channel": f"mailto:{holder}@example.test",
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
    service = RecallService(connection, clock)

    for user_id, role in (
        ("coord", "coordinator"),
        ("boss", "approver"),
        ("field-1", "field"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 主数据：电芯批次、电芯、模组、电池包、场站。
    assets = (
        ("lot-cell-001", "cell_lot", "0"),
        ("cell-1", "cell", "0"),
        ("cell-2", "cell", "0"),
        ("mod-1", "module", "50"),
        ("mod-2", "module", "50"),
        ("pack-1", "battery_pack", "100"),
        ("pack-2", "battery_pack", "100"),
        ("pack-3", "battery_pack", "100"),
        ("site-alpha", "site", "500"),
    )
    for asset_id, kind, capacity in assets:
        service.register_asset("coord", asset_id, kind, capacity, True)

    # 冻结谱系修订 1：批次电芯装入 mod-1，再装入 pack-1，安装到场站。
    service.record_genealogy_edges("coord", [
        _edge("e-lot-cell1", "lot-cell-001", "cell-1", "assembly", "2026-08-02T08:00:00Z"),
        _edge("e-mod1-cell1", "mod-1", "cell-1", "assembly", "2026-08-03T08:00:00Z"),
        _edge("e-pack1-mod1", "pack-1", "mod-1", "installed", "2026-08-05T08:00:00Z"),
        _edge("e-site-pack1", "site-alpha", "pack-1", "installed", "2026-08-10T08:00:00Z"),
    ])

    # 有效所有权：pack-1 最初由东部分公司持有；cell-2 故意不登记持有人。
    service.record_ownership("coord", [
        _owner("lot-cell-001", 1, "manufacturer"),
        _owner("cell-1", 1, "manufacturer"),
        _owner("mod-1", 1, "manufacturer"),
        _owner("pack-1", 1, "operator-east"),
        _owner("site-alpha", 1, "util-north"),
    ])

    # 供应商批次风险通知与召回启动：从冻结谱系+有效所有权计算初始范围 v1。
    service.publish_notice("coord", {
        "notice_id": "notice-2026-09",
        "supplier_id": "supplier-x",
        "component_lot_id": "lot-cell-001",
        "title": "B 批次内隔膜缺陷风险",
        "risk_summary": "特定批次隔膜存在热失控隐患，需要召回排查。",
        "issued_at": "2026-09-19T00:00:00Z",
        "evidence_sha256": "a" * 64,
    })
    initiated = service.initiate_recall("coord", "recall-001", "notice-2026-09", 72)

    # pack-1：签收时仍在东部分公司名下。
    ack = service.record_action(
        "field-1", "recall-001", "pack-1", "acknowledge", "ack-pack1-1",
        evidence_sha256="b" * 64, note="持有人电话确认",
    )
    # 幂等重放：同一回执键重复提交返回原结果。
    ack_replay = service.record_action(
        "field-1", "recall-001", "pack-1", "acknowledge", "ack-pack1-1",
    )
    # 新键重复回执：登记 duplicate，不倒退状态。
    ack_dup = service.record_action(
        "field-1", "recall-001", "pack-1", "acknowledge", "ack-pack1-dup",
    )

    # pack-1 进入转售流程：所有权版本 v2 为转移中，召回约束保留，义务方随版本更新。
    service.record_ownership("coord", [
        {**_owner("pack-1", 2, "operator-east", "in_transfer"),
         "effective_at": "2026-09-21T00:00:00Z",
         "contact_channel": "mailto:handover@example.test"},
    ])
    quarantine = service.record_action(
        "field-1", "recall-001", "pack-1", "quarantine", "qua-pack1-1",
        note="转移在途仍执行远程隔离令",
    )
    service.record_ownership("coord", [
        {**_owner("pack-1", 3, "operator-south", "held"),
         "effective_at": "2026-09-22T00:00:00Z"},
    ])

    # 乱序回执：尚未现场检查就回报返厂，登记 out_of_order，阶段保持 inspect。
    out_of_order = service.record_action(
        "field-1", "recall-001", "pack-1", "return_to_oem", "ret-pack1-bad",
    )

    # 新谱系证据（修订 2）：同批次 cell-2 经返修拆出、重装入 mod-2/pack-3，
    # 并曾装入 pack-2；风险沿返修与拆分路径扩散。
    service.record_genealogy_edges("coord", [
        _edge("e-lot-cell2", "lot-cell-001", "cell-2", "assembly", "2026-08-02T09:00:00Z"),
        _edge("e-pack2-cell2", "pack-2", "cell-2", "repair", "2026-09-01T09:00:00Z"),
        _edge("e-mod2-cell2", "mod-2", "cell-2", "dismantle", "2026-09-05T09:00:00Z"),
        _edge("e-pack3-mod2", "pack-3", "mod-2", "lease", "2026-09-10T09:00:00Z"),
    ])
    service.record_ownership("coord", [
        _owner("mod-2", 1, "lessor-z"),
        _owner("pack-2", 1, "operator-east"),
        _owner("pack-3", 1, "lessee-y"),
    ])
    expanded = service.expand_scope("coord", "recall-001", "返修与拆分证据显示风险扩散到 pack-2/pack-3")

    # pack-3 承租方无法联系：进入升级队列，措施挂起但约束不解除。
    unreachable = service.mark_unreachable(
        "coord", "recall-001", "pack-3", "租赁方联系方式失效，多次致电无应答"
    )

    # 时间越过 SLA：扫描逾期动作（pack-1 停在 inspect 等）。
    clock.advance(hours=100)
    overdue = service.sweep_overdue("coord", "recall-001")

    # 重新联系到承租方并解决升级；pack-1 继续完成检查、返厂、解除。
    service.resolve_escalation("coord", unreachable["escalation_id"], "取得新联系方式", reengage=True)
    service.record_action("field-1", "recall-001", "pack-1", "inspect", "ins-pack1-1")
    returned = service.record_action("field-1", "recall-001", "pack-1", "return_to_oem", "ret-pack1-1")
    released = service.record_action("field-1", "recall-001", "pack-1", "release", "rel-pack1-1")

    # 范围缩减（排除经供应商复检确认无风险的剩余库存批次本体）需要独立批准。
    reduction = service.request_scope_reduction(
        "coord", "recall-001", ["lot-cell-001"], "供应商复检确认该批剩余库存不受隔膜缺陷影响"
    )
    self_approval_blocked = False
    try:
        service.review_scope_reduction("coord", "recall-001", reduction["scope_version"], True, "自批")
    except Exception:
        self_approval_blocked = True
    approved = service.review_scope_reduction(
        "boss", "recall-001", reduction["scope_version"], True, "证据链完整，同意缩减"
    )

    # 缩减后历史通知事实保留，但措施停止推进。
    detail_lot = service.asset_detail("audit", "recall-001", "lot-cell-001")
    reduction_stops_actions = False
    try:
        service.record_action("field-1", "recall-001", "lot-cell-001", "acknowledge", "ack-lot-x")
    except Exception:
        reduction_stops_actions = True

    dashboard = service.dashboard("coord", "recall-001")
    recomputed_v1 = service.recompute_scope("audit", "recall-001", 1)
    recomputed_v2 = service.recompute_scope("audit", "recall-001", 2)
    recomputed_v3 = service.recompute_scope("audit", "recall-001", 3)
    history = service.recall_history("audit", "recall-001")
    chain = service.audit_chain("audit")
    schema = inspect_schema(connection)
    connection.close()

    return {
        "status": "ok",
        "initial_scope_version": initiated["scope_version"],
        "initial_asset_count": initiated["asset_count"],
        "ack_state": ack["state"],
        "ack_replay_replayed": ack_replay["replayed"],
        "ack_duplicate_state": ack_dup["state"],
        "quarantine_in_transfer_holder": quarantine["holder_id"],
        "quarantine_in_transfer_mode": quarantine["owner_mode"],
        "out_of_order_state": out_of_order["state"],
        "out_of_order_stage": out_of_order["current_stage"],
        "expanded_version": expanded["scope_version"],
        "new_assets": expanded["new_assets"],
        "unreachable_escalation": unreachable["escalation_id"],
        "overdue_opened": overdue["opened"],
        "pack1_final_stage": released["next_stage"],
        "return_receipt_state": returned["state"],
        "reduction_version": reduction["scope_version"],
        "self_approval_blocked": self_approval_blocked,
        "reduction_status": approved["status"],
        "notice_history_preserved_after_reduction": any(
            item["stage"] == "notify" and item["state"] == "done"
            for item in detail_lot["receipts"]
        ),
        "reduction_stops_actions": reduction_stops_actions,
        "current_scope_version": dashboard["current_scope_version"],
        "included_assets": dashboard["scope"]["included_assets"],
        "affected_capacity_kwh": dashboard["capacity_kwh"]["affected"],
        "running_affected_capacity_kwh": dashboard["capacity_kwh"]["running_affected"],
        "holder_count": len(dashboard["current_holders"]),
        "open_escalations": dashboard["open_escalations"],
        "version_reasons": [v["reason"] for v in dashboard["versions"]],
        "recompute_v1_matches": recomputed_v1["content_matches"],
        "recompute_v2_matches": recomputed_v2["content_matches"],
        "recompute_v3_matches": recomputed_v3["content_matches"],
        "history_events": len(history["events"]),
        "audit_chain": chain,
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电芯批次召回编排离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
