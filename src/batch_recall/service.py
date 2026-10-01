"""批次召回编排的领域用例。

设计要点：
- 谱系证据与所有权版本只增不改；范围版本记录输入水位（谱系修订、截止时刻、
  父版本），审计可从原始证据逐级重放复算。
- 每个资产的六类措施独立推进，回执 append-only；重复或乱序回执只登记不推进，
  已完成状态不会倒退。
- 缩减产生待批准版本，批准前不影响任何在办措施；批准后历史通知与完成事实保留。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat, utc_text
from .contracts import (
    ACTION_STAGES,
    STAGE_INDEX,
    GenealogyEdge,
    OwnershipVersion,
    SupplierNotice,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, digest_value
from .scope import (
    Edge,
    build_entries,
    edge_from_row,
    effective_owners,
    quantize_capacity,
    scope_content,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "coordinator": {
        "asset.write", "genealogy.write", "ownership.write", "notice.write", "recall.write",
        "action.write", "escalation.write", "report.read",
    },
    "approver": {"scope.approve", "report.read", "audit.read"},
    "field": {"action.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}


class RecallService:
    """在单个 SQLite 连接上提供召回编排全部操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _due(self, sla_hours: int) -> str:
        return utc_text(self.clock.now() + timedelta(hours=sla_hours))

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM recall_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM recall_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO recall_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recall_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------- 主数据

    def register_asset(
        self,
        actor_id: str,
        asset_id: str,
        asset_kind: str,
        capacity_kwh: object = "0",
        running: bool = True,
    ) -> dict[str, Any]:
        self._require(actor_id, "asset.write")
        if not asset_id.strip() or not asset_kind.strip():
            raise ValidationFailed("资产编号与类型不能为空")
        capacity = self._decimal(capacity_kwh, "capacity_kwh")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recall_assets(asset_id,asset_kind,capacity_kwh,running,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (asset_id.strip(), asset_kind.strip(), quantize_capacity(capacity),
                     1 if running else 0, self._now()),
                )
                self._audit("asset", asset_id.strip(), "asset.registered", actor_id,
                            {"asset_kind": asset_kind, "capacity_kwh": quantize_capacity(capacity)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("资产编号已经存在") from exc
        return {"asset_id": asset_id.strip(), "asset_kind": asset_kind.strip(),
                "capacity_kwh": quantize_capacity(capacity), "running": bool(running)}

    def set_asset_running(self, actor_id: str, asset_id: str, running: bool) -> dict[str, Any]:
        self._require(actor_id, "asset.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE recall_assets SET running=? WHERE asset_id=?",
                (1 if running else 0, asset_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("资产不存在")
            self._audit("asset", asset_id, "asset.running_changed", actor_id, {"running": bool(running)})
        return {"asset_id": asset_id, "running": bool(running)}

    @staticmethod
    def _decimal(value: object, field: str) -> Decimal:
        if isinstance(value, bool):
            raise ValidationFailed(f"{field} 必须是数值")
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise ValidationFailed(f"{field} 必须是十进制数值") from exc
        if not result.is_finite() or result < 0:
            raise ValidationFailed(f"{field} 必须是非负有限数值")
        return result

    def record_genealogy_edges(self, actor_id: str, raw_edges: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """把一批谱系证据冻结为一个只增修订。"""

        self._require(actor_id, "genealogy.write")
        edges = [GenealogyEdge.from_dict(raw) for raw in raw_edges]
        if not edges:
            raise ValidationFailed("谱系证据不能为空")
        with transaction(self.connection, immediate=True):
            revision_row = self.connection.execute(
                "SELECT COALESCE(max(revision), 0) + 1 AS revision FROM genealogy_revisions"
            ).fetchone()
            revision = int(revision_row["revision"])
            now = self._now()
            for edge in edges:
                endpoints = self.connection.execute(
                    "SELECT count(DISTINCT asset_id) AS n FROM recall_assets WHERE asset_id IN (?, ?)",
                    (edge.parent_id, edge.child_id),
                ).fetchone()
                if int(endpoints["n"]) != 2:
                    raise NotFound(f"谱系边端点资产未登记: {edge.edge_id}")
                try:
                    self.connection.execute(
                        "INSERT INTO genealogy_edges(edge_id,parent_id,child_id,edge_kind,observed_at,"
                        "evidence_sha256,note,genealogy_revision,recorded_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (edge.edge_id, edge.parent_id, edge.child_id, edge.edge_kind, edge.observed_at,
                         edge.evidence_sha256, edge.note, revision, actor_id, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(f"谱系边编号冲突: {edge.edge_id}") from exc
            self.connection.execute(
                "INSERT INTO genealogy_revisions(revision,recorded_at,recorded_by,edge_count) "
                "VALUES(?,?,?,?)",
                (revision, now, actor_id, len(edges)),
            )
            self._audit("genealogy", str(revision), "genealogy.recorded", actor_id,
                        {"revision": revision, "edge_count": len(edges),
                         "edge_ids": [edge.edge_id for edge in edges]})
        return {"genealogy_revision": revision, "edge_count": len(edges)}

    def record_ownership(self, actor_id: str, raw_versions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """登记有效所有权版本；只增不改，转移中资产的召回约束随之保留。"""

        self._require(actor_id, "ownership.write")
        versions = [OwnershipVersion.from_dict(raw) for raw in raw_versions]
        if not versions:
            raise ValidationFailed("所有权版本不能为空")
        recorded: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            now = self._now()
            for item in versions:
                asset = self.connection.execute(
                    "SELECT 1 FROM recall_assets WHERE asset_id=?", (item.asset_id,)
                ).fetchone()
                if asset is None:
                    raise NotFound(f"资产不存在: {item.asset_id}")
                try:
                    self.connection.execute(
                        "INSERT INTO ownership_versions(asset_id,version,holder_id,mode,effective_at,"
                        "contact_channel,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (item.asset_id, item.version, item.holder_id, item.mode, item.effective_at,
                         item.contact_channel, actor_id, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(f"所有权版本冲突: {item.asset_id}@{item.version}") from exc
                self.connection.execute(
                    "UPDATE asset_actions SET latest_holder_id=?, latest_owner_version=?, updated_at=? "
                    "WHERE asset_id=? AND (latest_owner_version IS NULL OR latest_owner_version < ?)",
                    (item.holder_id, item.version, now, item.asset_id, item.version),
                )
                recorded.append({"asset_id": item.asset_id, "version": item.version,
                                 "holder_id": item.holder_id, "mode": item.mode})
            self._audit("ownership", "versions", "ownership.recorded", actor_id, {"versions": recorded})
        return {"recorded": len(recorded), "versions": recorded}

    # ------------------------------------------------------------- 通知召回

    def publish_notice(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "notice.write")
        notice = SupplierNotice.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supplier_notices(notice_id,supplier_id,component_lot_id,title,risk_summary,"
                    "issued_at,evidence_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (notice.notice_id, notice.supplier_id, notice.component_lot_id, notice.title,
                     notice.risk_summary, notice.issued_at, notice.evidence_sha256, actor_id, self._now()),
                )
                self._audit("notice", notice.notice_id, "notice.published", actor_id,
                            {"supplier_id": notice.supplier_id,
                             "component_lot_id": notice.component_lot_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("通知编号已经存在") from exc
        return {"notice_id": notice.notice_id, "component_lot_id": notice.component_lot_id}

    def _edges_at(self, revision: int) -> list[Edge]:
        rows = self.connection.execute(
            "SELECT * FROM genealogy_edges WHERE genealogy_revision<=? ORDER BY edge_id", (revision,)
        ).fetchall()
        return [edge_from_row(row) for row in rows]

    def _ownership_rows_at(self, as_of: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM ownership_versions WHERE effective_at<=? ORDER BY asset_id,version", (as_of,)
        ).fetchall()

    def _latest_scope_row(self, recall_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM scope_versions WHERE recall_id=? AND status='effective' "
            "ORDER BY scope_version DESC LIMIT 1",
            (recall_id,),
        ).fetchone()

    def _scope_entry_map(self, recall_id: str, version: int) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM scope_entries WHERE recall_id=? AND scope_version=?", (recall_id, version)
        ).fetchall()
        return {
            row["asset_id"]: {
                "asset_id": row["asset_id"],
                "include_state": row["include_state"],
                "added_in_version": row["added_in_version"],
                "match_path": json.loads(row["match_path_json"]),
                "first_noticed_at": row["first_noticed_at"],
            }
            for row in rows
        }

    def _basis_digest(
        self,
        *,
        change_kind: str,
        revision: int,
        as_of: str,
        reason: str,
        parent_content: str | None,
        excluded: Sequence[str] = (),
    ) -> str:
        edges = [
            {
                "edge_id": edge.edge_id,
                "parent_id": edge.parent_id,
                "child_id": edge.child_id,
                "edge_kind": edge.edge_kind,
                "observed_at": edge.observed_at,
                "evidence_sha256": edge.evidence_sha256,
                "genealogy_revision": edge.genealogy_revision,
            }
            for edge in self._edges_at(revision)
        ]
        ownership = [
            {
                "asset_id": row["asset_id"],
                "version": row["version"],
                "holder_id": row["holder_id"],
                "mode": row["mode"],
                "effective_at": row["effective_at"],
                "contact_channel": row["contact_channel"],
            }
            for row in self._ownership_rows_at(as_of)
        ]
        return digest_value({
            "change_kind": change_kind,
            "genealogy_revision": revision,
            "as_of": as_of,
            "reason": reason,
            "parent_content_sha256": parent_content,
            "excluded": sorted(excluded),
            "edges": edges,
            "ownership": ownership,
        })

    def initiate_recall(self, actor_id: str, recall_id: str, notice_id: str, sla_hours: int) -> dict[str, Any]:
        """发布召回并从冻结谱系与有效所有权计算初始范围（版本 1）。"""

        self._require(actor_id, "recall.write")
        if isinstance(sla_hours, bool) or not isinstance(sla_hours, int) or sla_hours <= 0:
            raise ValidationFailed("sla_hours 必须是正整数")
        notice = self.connection.execute(
            "SELECT * FROM supplier_notices WHERE notice_id=?", (notice_id,)
        ).fetchone()
        if notice is None:
            raise NotFound("供应商通知不存在")
        as_of = self._now()
        revision_row = self.connection.execute(
            "SELECT COALESCE(max(revision), 0) AS revision FROM genealogy_revisions"
        ).fetchone()
        revision = int(revision_row["revision"])
        edges = self._edges_at(revision)
        owners = effective_owners(self._ownership_rows_at(as_of), as_of)
        entries = build_entries(
            change_kind="initial",
            seed=notice["component_lot_id"],
            edges=edges,
            parents={},
            excluded=None,
            scope_version=1,
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recalls(recall_id,notice_id,component_lot_id,state,sla_hours,"
                    "current_scope_version,created_by,created_at) VALUES(?,?,?, 'active', ?,1,?,?)",
                    (recall_id, notice_id, notice["component_lot_id"], sla_hours, actor_id, as_of),
                )
                self._write_scope_version(
                    recall_id=recall_id,
                    scope_version=1,
                    change_kind="initial",
                    status="effective",
                    reason="供应商批次风险通知初始范围",
                    revision=revision,
                    as_of=as_of,
                    entries=entries,
                    parent_version=None,
                    parent_content=None,
                    actor_id=actor_id,
                    decided_by=actor_id,
                )
                self._notify_scope(recall_id, entries, 1, sla_hours, as_of, actor_id)
                self._audit("recall", recall_id, "recall.initiated", actor_id,
                            {"notice_id": notice_id, "scope_version": 1,
                             "asset_count": sum(1 for e in entries if e["include_state"] == "included")})
        except sqlite3.IntegrityError as exc:
            raise Conflict("召回编号冲突或通知已被召回引用") from exc
        return {"recall_id": recall_id, "scope_version": 1, "status": "effective",
                "asset_count": len(entries)}

    def _write_scope_version(
        self,
        *,
        recall_id: str,
        scope_version: int,
        change_kind: str,
        status: str,
        reason: str,
        revision: int,
        as_of: str,
        entries: Sequence[Mapping[str, Any]],
        parent_version: int | None,
        parent_content: str | None,
        actor_id: str,
        decided_by: str | None,
        excluded: Sequence[str] = (),
        decision_note: str | None = None,
    ) -> str:
        content = scope_content(
            recall_id=recall_id,
            scope_version=scope_version,
            change_kind=change_kind,
            genealogy_revision=revision,
            as_of=as_of,
            entries=entries,
        )
        content_sha = digest_value(content)
        basis_sha = self._basis_digest(
            change_kind=change_kind,
            revision=revision,
            as_of=as_of,
            reason=reason,
            parent_content=parent_content,
            excluded=excluded,
        )
        decided_at = self._now() if status == "effective" else None
        self.connection.execute(
            "INSERT INTO scope_versions(scope_version,recall_id,change_kind,status,reason,"
            "genealogy_revision,as_of,basis_sha256,content_sha256,parent_scope_version,"
            "created_by,created_at,decided_by,decided_at,decision_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope_version, recall_id, change_kind, status, reason, revision, as_of,
             basis_sha, content_sha, parent_version, actor_id, self._now(),
             decided_by, decided_at, decision_note),
        )
        for entry in entries:
            self.connection.execute(
                "INSERT INTO scope_entries(recall_id,scope_version,asset_id,include_state,"
                "added_in_version,match_path_json,first_noticed_at) VALUES(?,?,?,?,?,?,?)",
                (recall_id, scope_version, entry["asset_id"], entry["include_state"],
                 entry["added_in_version"], canonical_json(entry["match_path"]),
                 entry["first_noticed_at"]),
            )
        return content_sha

    def _recall(self, recall_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM recalls WHERE recall_id=?", (recall_id,)).fetchone()
        if row is None:
            raise NotFound("召回不存在")
        return row

    def _notify_scope(
        self,
        recall_id: str,
        entries: Sequence[Mapping[str, Any]],
        scope_version: int,
        sla_hours: int,
        now: str,
        actor_id: str,
    ) -> None:
        """为首次进入范围的资产建立措施链并登记通知事实。"""

        for entry in entries:
            if entry["include_state"] != "included":
                continue
            asset_id = entry["asset_id"]
            owner = self.connection.execute(
                "SELECT * FROM ownership_versions WHERE asset_id=? ORDER BY version DESC LIMIT 1",
                (asset_id,),
            ).fetchone()
            holder_id = None if owner is None else owner["holder_id"]
            owner_version = None if owner is None else int(owner["version"])
            existing = self.connection.execute(
                "SELECT * FROM asset_actions WHERE recall_id=? AND asset_id=?",
                (recall_id, asset_id),
            ).fetchone()
            if existing is not None:
                if existing["in_scope"]:
                    # 已在推进的资产不受新版本影响：SLA 与措施阶段保持不变。
                    continue
                # 曾被缩减的资产随新证据重新纳入：恢复在范围标记与 SLA，不重发通知。
                self.connection.execute(
                    "UPDATE asset_actions SET in_scope=1, blocked=0, unreachable_flag=0, "
                    "current_due_at=?, updated_at=? WHERE recall_id=? AND asset_id=?",
                    (self._due(sla_hours), now, recall_id, asset_id),
                )
                self.connection.execute(
                    "UPDATE scope_entries SET first_noticed_at=COALESCE(first_noticed_at, ?) "
                    "WHERE recall_id=? AND scope_version=? AND asset_id=?",
                    (now, recall_id, scope_version, asset_id),
                )
                continue
            cursor = self.connection.execute(
                "INSERT INTO action_receipts(recall_id,asset_id,stage,state,idempotency_key,"
                "holder_id,owner_version,note,recorded_by,recorded_at) VALUES(?,?, 'notify','done',"
                "NULL,?,?, '召回启动通知', ?,?)",
                (recall_id, asset_id, holder_id, owner_version, actor_id, now),
            )
            receipt_id = int(cursor.lastrowid)
            self.connection.execute(
                "INSERT INTO action_stage_completions(recall_id,asset_id,stage,receipt_id,"
                "due_at,completed_at,holder_id,owner_version) VALUES(?,?, 'notify',?,NULL,?,?,?)",
                (recall_id, asset_id, receipt_id, now, holder_id, owner_version),
            )
            self.connection.execute(
                "INSERT INTO asset_actions(recall_id,asset_id,scope_version,current_stage,in_scope,"
                "current_due_at,blocked,unreachable_flag,first_notice_version,latest_holder_id,"
                "latest_owner_version,updated_at) VALUES(?,?,?, 'acknowledge',1,?,0,0,?,?,?,?)",
                (recall_id, asset_id, scope_version, self._due(sla_hours), scope_version,
                 holder_id, owner_version, now),
            )
            self.connection.execute(
                "UPDATE scope_entries SET first_noticed_at=? WHERE recall_id=? AND scope_version=? AND asset_id=?",
                (now, recall_id, scope_version, asset_id),
            )
            if owner is None:
                self.connection.execute(
                    "INSERT INTO escalations(recall_id,asset_id,stage,reason,status,note,"
                    "opened_by,opened_at) VALUES(?,?,'notify','unreachable','open',"
                    "'无有效所有权版本，无法确定持有人',?,?)",
                    (recall_id, asset_id, actor_id, now),
                )
                self.connection.execute(
                    "UPDATE asset_actions SET blocked=1, unreachable_flag=1 "
                    "WHERE recall_id=? AND asset_id=?",
                    (recall_id, asset_id),
                )

    def expand_scope(self, actor_id: str, recall_id: str, reason: str) -> dict[str, Any]:
        """新的谱系证据表明风险扩散时，生成可审核的有效新版本。"""

        self._require(actor_id, "recall.write")
        recall = self._recall(recall_id)
        self._no_pending_reduction(recall_id)
        parent = self._latest_scope_row(recall_id)
        if parent is None:
            raise InvalidState("召回尚未初始化范围")
        parent_map = self._scope_entry_map(recall_id, int(parent["scope_version"]))
        revision_row = self.connection.execute(
            "SELECT COALESCE(max(revision), 0) AS revision FROM genealogy_revisions"
        ).fetchone()
        revision = int(revision_row["revision"])
        as_of = self._now()
        edges = self._edges_at(revision)
        entries = build_entries(
            change_kind="expand",
            seed=recall["component_lot_id"],
            edges=edges,
            parents=parent_map,
            excluded=None,
            scope_version=int(parent["scope_version"]) + 1,
        )
        previous_included = {a for a, e in parent_map.items() if e["include_state"] == "included"}
        new_assets = [
            e["asset_id"] for e in entries
            if e["include_state"] == "included" and e["asset_id"] not in previous_included
        ]
        if not new_assets and revision == int(parent["genealogy_revision"]):
            raise InvalidState("新的谱系证据未改变召回范围")
        if not new_assets:
            raise InvalidState("最新谱系修订未引入新的受影响资产")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE scope_versions SET status='superseded' WHERE recall_id=? AND status='effective'",
                (recall_id,),
            )
            content_sha = self._write_scope_version(
                recall_id=recall_id,
                scope_version=int(parent["scope_version"]) + 1,
                change_kind="expand",
                status="effective",
                reason=reason,
                revision=revision,
                as_of=as_of,
                entries=entries,
                parent_version=int(parent["scope_version"]),
                parent_content=parent["content_sha256"],
                actor_id=actor_id,
                decided_by=actor_id,
            )
            self._notify_scope(recall_id, entries, int(parent["scope_version"]) + 1,
                               int(recall["sla_hours"]), as_of, actor_id)
            self.connection.execute(
                "UPDATE recalls SET current_scope_version=? WHERE recall_id=?",
                (int(parent["scope_version"]) + 1, recall_id),
            )
            self._audit("recall", recall_id, "scope.expanded", actor_id,
                        {"scope_version": int(parent["scope_version"]) + 1,
                         "new_assets": new_assets, "reason": reason, "content_sha256": content_sha})
        return {"recall_id": recall_id, "scope_version": int(parent["scope_version"]) + 1,
                "status": "effective", "new_assets": new_assets, "reason": reason}

    def _no_pending_reduction(self, recall_id: str) -> None:
        pending = self.connection.execute(
            "SELECT scope_version FROM scope_versions WHERE recall_id=? AND status='pending_approval'",
            (recall_id,),
        ).fetchone()
        if pending is not None:
            raise InvalidState(f"存在待批准的范围缩减版本 {pending['scope_version']}")

    def request_scope_reduction(
        self, actor_id: str, recall_id: str, asset_ids: Sequence[str], reason: str
    ) -> dict[str, Any]:
        """范围缩减必须走独立批准；生效前不影响任何措施。"""

        self._require(actor_id, "recall.write")
        recall = self._recall(recall_id)
        self._no_pending_reduction(recall_id)
        if not asset_ids:
            raise ValidationFailed("缩减资产列表不能为空")
        if not reason.strip():
            raise ValidationFailed("缩减原因不能为空")
        parent = self._latest_scope_row(recall_id)
        if parent is None:
            raise InvalidState("召回尚未初始化范围")
        parent_map = self._scope_entry_map(recall_id, int(parent["scope_version"]))
        target_version = int(parent["scope_version"]) + 1
        entries = build_entries(
            change_kind="reduce",
            seed=recall["component_lot_id"],
            edges=[],
            parents=parent_map,
            excluded=frozenset(asset_ids),
            scope_version=target_version,
        )
        excluded = [
            entry["asset_id"] for entry in entries if entry["include_state"] == "excluded"
        ]
        as_of = self._now()
        with transaction(self.connection, immediate=True):
            content_sha = self._write_scope_version(
                recall_id=recall_id,
                scope_version=target_version,
                change_kind="reduce",
                status="pending_approval",
                reason=reason.strip(),
                revision=int(parent["genealogy_revision"]),
                as_of=as_of,
                entries=entries,
                parent_version=int(parent["scope_version"]),
                parent_content=parent["content_sha256"],
                actor_id=actor_id,
                decided_by=None,
                excluded=excluded,
            )
            self._audit("recall", recall_id, "scope.reduction_requested", actor_id,
                        {"scope_version": target_version, "excluded": excluded,
                         "reason": reason.strip(), "content_sha256": content_sha})
        return {"recall_id": recall_id, "scope_version": target_version,
                "status": "pending_approval", "excluded": excluded}

    def review_scope_reduction(
        self, actor_id: str, recall_id: str, scope_version: int, approve: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "scope.approve")
        row = self.connection.execute(
            "SELECT * FROM scope_versions WHERE recall_id=? AND scope_version=? AND change_kind='reduce'",
            (recall_id, scope_version),
        ).fetchone()
        if row is None:
            raise NotFound("待批准缩减版本不存在")
        if row["status"] != "pending_approval":
            raise InvalidState("该缩减版本已经处理")
        if row["created_by"] == actor_id:
            raise Forbidden("申请人不能批准自己的范围缩减")
        now = self._now()
        with transaction(self.connection, immediate=True):
            if approve:
                parent = self._latest_scope_row(recall_id)
                if parent is None or int(parent["scope_version"]) != int(row["parent_scope_version"]):
                    raise InvalidState("父范围版本已变化，请重新提交缩减")
                self.connection.execute(
                    "UPDATE scope_versions SET status='effective', decided_by=?, decided_at=?, "
                    "decision_note=? WHERE recall_id=? AND scope_version=? AND status='pending_approval'",
                    (actor_id, now, note, recall_id, scope_version),
                )
                self.connection.execute(
                    "UPDATE scope_versions SET status='superseded' WHERE recall_id=? "
                    "AND status='effective' AND scope_version<>?",
                    (recall_id, scope_version),
                )
                excluded_rows = self.connection.execute(
                    "SELECT asset_id FROM scope_entries WHERE recall_id=? AND scope_version=? "
                    "AND include_state='excluded'",
                    (recall_id, scope_version),
                ).fetchall()
                for item in excluded_rows:
                    # 约束解除针对未来措施；通知与完成历史原样保留。
                    self.connection.execute(
                        "UPDATE asset_actions SET in_scope=0, current_due_at=NULL, blocked=0, "
                        "unreachable_flag=0, updated_at=? WHERE recall_id=? AND asset_id=?",
                        (now, recall_id, item["asset_id"]),
                    )
                self.connection.execute(
                    "UPDATE recalls SET current_scope_version=? WHERE recall_id=?",
                    (scope_version, recall_id),
                )
                event_type = "scope.reduction_approved"
            else:
                self.connection.execute(
                    "UPDATE scope_versions SET status='superseded', decided_by=?, decided_at=?, "
                    "decision_note=? WHERE recall_id=? AND scope_version=? AND status='pending_approval'",
                    (actor_id, now, note, recall_id, scope_version),
                )
                event_type = "scope.reduction_rejected"
            self._audit("recall", recall_id, event_type, actor_id,
                        {"scope_version": scope_version, "approve": bool(approve), "note": note})
        return {"recall_id": recall_id, "scope_version": scope_version,
                "status": "effective" if approve else "superseded"}

    # ------------------------------------------------------------- 措施链

    def record_action(
        self,
        actor_id: str,
        recall_id: str,
        asset_id: str,
        stage: str,
        idempotency_key: str,
        *,
        evidence_sha256: str | None = None,
        note: str = "",
        reject: bool = False,
    ) -> dict[str, Any]:
        """登记某资产某阶段的回执；重复与乱序回执不倒退已完成状态。"""

        self._require(actor_id, "action.write")
        if stage not in STAGE_INDEX:
            raise ValidationFailed("未知措施阶段")
        if not idempotency_key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        if evidence_sha256 is not None and len(evidence_sha256.strip()) != 64:
            raise ValidationFailed("evidence_sha256 必须是 64 位")
        action = self.connection.execute(
            "SELECT * FROM asset_actions WHERE recall_id=? AND asset_id=?",
            (recall_id, asset_id),
        ).fetchone()
        if action is None:
            raise NotFound("该资产不在召回范围")
        if not action["in_scope"]:
            raise InvalidState("资产已不在当前召回范围，措施停止推进，历史记录保留")
        recall = self._recall(recall_id)

        replayed = self.connection.execute(
            "SELECT * FROM action_receipts WHERE recall_id=? AND asset_id=? AND stage=? "
            "AND idempotency_key=?",
            (recall_id, asset_id, stage, idempotency_key.strip()),
        ).fetchone()
        if replayed is not None:
            refreshed = self.connection.execute(
                "SELECT current_stage FROM asset_actions WHERE recall_id=? AND asset_id=?",
                (recall_id, asset_id),
            ).fetchone()
            return {"recall_id": recall_id, "asset_id": asset_id, "stage": stage,
                    "state": replayed["state"], "replayed": True,
                    "current_stage": refreshed["current_stage"]}

        owner = self.connection.execute(
            "SELECT * FROM ownership_versions WHERE asset_id=? ORDER BY version DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        holder_id = None if owner is None else owner["holder_id"]
        owner_version = None if owner is None else int(owner["version"])
        now = self._now()

        with transaction(self.connection, immediate=True):
            if reject:
                cursor = self.connection.execute(
                    "INSERT INTO action_receipts(recall_id,asset_id,stage,state,idempotency_key,"
                    "holder_id,owner_version,evidence_sha256,note,recorded_by,recorded_at) "
                    "VALUES(?,?,?, 'rejected',?,?,?,?,?,?,?)",
                    (recall_id, asset_id, stage, idempotency_key.strip(), holder_id, owner_version,
                     evidence_sha256, note, actor_id, now),
                )
                self.connection.execute(
                    "UPDATE asset_actions SET blocked=1, latest_holder_id=COALESCE(?,latest_holder_id), "
                    "latest_owner_version=COALESCE(?,latest_owner_version), updated_at=? "
                    "WHERE recall_id=? AND asset_id=?",
                    (holder_id, owner_version, now, recall_id, asset_id),
                )
                self._open_escalation(recall_id, asset_id, stage, "rejected",
                                      f"持有人拒绝 {stage}: {note}", actor_id, now)
                self._audit("asset", f"{recall_id}/{asset_id}", "action.rejected", actor_id,
                            {"stage": stage, "holder_id": holder_id, "note": note})
                return {"recall_id": recall_id, "asset_id": asset_id, "stage": stage,
                        "state": "rejected", "replayed": False, "receipt_id": int(cursor.lastrowid)}

            done = self.connection.execute(
                "SELECT receipt_id FROM action_stage_completions WHERE recall_id=? AND asset_id=? AND stage=?",
                (recall_id, asset_id, stage),
            ).fetchone()
            if done is not None:
                # 重复回执：登记但不产生任何状态变化。
                state = "duplicate"
                self._insert_receipt(recall_id, asset_id, stage, state, idempotency_key.strip(),
                                     holder_id, owner_version, evidence_sha256, note, actor_id, now)
                self._audit("asset", f"{recall_id}/{asset_id}", "action.duplicate", actor_id,
                            {"stage": stage})
                return {"recall_id": recall_id, "asset_id": asset_id, "stage": stage,
                        "state": "duplicate", "replayed": False, "current_stage": action["current_stage"]}

            expected_stage = action["current_stage"]
            if expected_stage == "closed" or stage != expected_stage:
                # 乱序回执（跳阶段或措施已关闭）：登记但不推进。
                state = "out_of_order"
                self._insert_receipt(recall_id, asset_id, stage, state, idempotency_key.strip(),
                                     holder_id, owner_version, evidence_sha256, note, actor_id, now)
                self._audit("asset", f"{recall_id}/{asset_id}", "action.out_of_order", actor_id,
                            {"stage": stage, "expected_stage": expected_stage})
                return {"recall_id": recall_id, "asset_id": asset_id, "stage": stage,
                        "state": "out_of_order", "replayed": False, "current_stage": expected_stage}

            cursor = self._insert_receipt(recall_id, asset_id, stage, "done", idempotency_key.strip(),
                                          holder_id, owner_version, evidence_sha256, note, actor_id, now)
            receipt_id = int(cursor.lastrowid)
            self.connection.execute(
                "INSERT INTO action_stage_completions(recall_id,asset_id,stage,receipt_id,due_at,"
                "completed_at,holder_id,owner_version) VALUES(?,?,?,?,?,?,?,?)",
                (recall_id, asset_id, stage, receipt_id, action["current_due_at"], now,
                 holder_id, owner_version),
            )
            current_index = STAGE_INDEX[stage]
            if current_index == len(ACTION_STAGES) - 1:
                next_stage = "closed"
                next_due = None
            else:
                next_stage = ACTION_STAGES[current_index + 1]
                next_due = self._due(int(recall["sla_hours"]))
            self.connection.execute(
                "UPDATE asset_actions SET current_stage=?, current_due_at=?, blocked=0, "
                "unreachable_flag=0, latest_holder_id=COALESCE(?,latest_holder_id), "
                "latest_owner_version=COALESCE(?,latest_owner_version), updated_at=? "
                "WHERE recall_id=? AND asset_id=?",
                (next_stage, next_due, holder_id, owner_version, now, recall_id, asset_id),
            )
            self._audit("asset", f"{recall_id}/{asset_id}", "action.completed", actor_id,
                        {"stage": stage, "next_stage": next_stage, "holder_id": holder_id,
                         "owner_mode": None if owner is None else owner["mode"]})
            return {"recall_id": recall_id, "asset_id": asset_id, "stage": stage,
                    "state": "done", "replayed": False, "next_stage": next_stage,
                    "holder_id": holder_id, "owner_mode": None if owner is None else owner["mode"]}

    def _insert_receipt(self, *args: Any) -> sqlite3.Cursor:
        return self.connection.execute(
            "INSERT INTO action_receipts(recall_id,asset_id,stage,state,idempotency_key,"
            "holder_id,owner_version,evidence_sha256,note,recorded_by,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            args,
        )

    # ------------------------------------------------------------- 升级队列

    def _open_escalation(
        self, recall_id: str, asset_id: str, stage: str, reason: str, note: str,
        actor_id: str, now: str,
    ) -> int:
        existing = self.connection.execute(
            "SELECT escalation_id FROM escalations WHERE recall_id=? AND asset_id=? AND stage=? "
            "AND reason=? AND status='open'",
            (recall_id, asset_id, stage, reason),
        ).fetchone()
        if existing is not None:
            return int(existing["escalation_id"])
        cursor = self.connection.execute(
            "INSERT INTO escalations(recall_id,asset_id,stage,reason,status,note,opened_by,opened_at) "
            "VALUES(?,?,?,?, 'open', ?,?,?)",
            (recall_id, asset_id, stage, reason, note, actor_id, now),
        )
        return int(cursor.lastrowid)

    def mark_unreachable(
        self, actor_id: str, recall_id: str, asset_id: str, note: str
    ) -> dict[str, Any]:
        """无法联系的持有人进入升级队列，措施挂起但不解除召回约束。"""

        self._require(actor_id, "escalation.write")
        action = self.connection.execute(
            "SELECT * FROM asset_actions WHERE recall_id=? AND asset_id=?",
            (recall_id, asset_id),
        ).fetchone()
        if action is None:
            raise NotFound("该资产不在召回范围")
        now = self._now()
        with transaction(self.connection, immediate=True):
            escalation_id = self._open_escalation(
                recall_id, asset_id, action["current_stage"] if action["current_stage"] != "closed" else "notify",
                "unreachable", note, actor_id, now,
            )
            self.connection.execute(
                "UPDATE asset_actions SET blocked=1, unreachable_flag=1, updated_at=? "
                "WHERE recall_id=? AND asset_id=? AND in_scope=1",
                (now, recall_id, asset_id),
            )
            self._audit("asset", f"{recall_id}/{asset_id}", "holder.unreachable", actor_id,
                        {"escalation_id": escalation_id, "note": note})
        return {"escalation_id": escalation_id, "status": "open", "reason": "unreachable"}

    def resolve_escalation(
        self, actor_id: str, escalation_id: int, note: str, *, reengage: bool = False
    ) -> dict[str, Any]:
        self._require(actor_id, "escalation.write")
        row = self.connection.execute(
            "SELECT * FROM escalations WHERE escalation_id=?", (escalation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("升级记录不存在")
        if row["status"] != "open":
            raise InvalidState("升级记录已关闭")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE escalations SET status='resolved', resolved_by=?, resolved_at=?, "
                "resolution_note=? WHERE escalation_id=? AND status='open'",
                (actor_id, now, note, escalation_id),
            )
            if reengage:
                recall = self._recall(row["recall_id"])
                self.connection.execute(
                    "UPDATE asset_actions SET blocked=0, unreachable_flag=0, "
                    "current_due_at=COALESCE(current_due_at, ?), updated_at=? "
                    "WHERE recall_id=? AND asset_id=? AND in_scope=1",
                    (self._due(int(recall["sla_hours"])), now, row["recall_id"], row["asset_id"]),
                )
            self._audit("escalation", str(escalation_id), "escalation.resolved", actor_id,
                        {"reengage": bool(reengage), "note": note})
        return {"escalation_id": escalation_id, "status": "resolved", "reengage": bool(reengage)}

    def list_escalations(self, actor_id: str, recall_id: str, status: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        if status is not None and status not in {"open", "resolved"}:
            raise ValidationFailed("status 必须是 open 或 resolved")
        sql = "SELECT * FROM escalations WHERE recall_id=?"
        params: list[Any] = [recall_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY opened_at, escalation_id"
        rows = self.connection.execute(sql, params).fetchall()
        return {"recall_id": recall_id, "escalations": [dict(row) for row in rows]}

    def sweep_overdue(self, actor_id: str, recall_id: str | None = None) -> dict[str, Any]:
        """把超过当前阶段 SLA 仍未完成的动作送入逾期升级队列。"""

        self._require(actor_id, "escalation.write")
        now = self._now()
        sql = (
            "SELECT a.*, r.sla_hours FROM asset_actions a JOIN recalls r ON r.recall_id=a.recall_id "
            "WHERE a.in_scope=1 AND a.current_due_at IS NOT NULL AND a.current_due_at<? "
            "AND a.current_stage<>'closed'"
        )
        params: list[Any] = [now]
        if recall_id is not None:
            sql += " AND a.recall_id=?"
            params.append(recall_id)
        opened: list[int] = []
        with transaction(self.connection, immediate=True):
            for row in self.connection.execute(sql, params).fetchall():
                already = self.connection.execute(
                    "SELECT 1 FROM escalations WHERE recall_id=? AND asset_id=? AND status='open' "
                    "AND reason IN ('overdue','unreachable','rejected')",
                    (row["recall_id"], row["asset_id"]),
                ).fetchone()
                if already is not None:
                    continue
                escalation_id = self._open_escalation(
                    row["recall_id"], row["asset_id"], row["current_stage"], "overdue",
                    f"阶段 {row['current_stage']} 超过 SLA({row['sla_hours']} 小时)", actor_id, now,
                )
                self.connection.execute(
                    "UPDATE asset_actions SET blocked=1, updated_at=? WHERE recall_id=? AND asset_id=?",
                    (now, row["recall_id"], row["asset_id"]),
                )
                opened.append(escalation_id)
            if opened:
                self._audit("recall", recall_id or "*", "overdue.swept", actor_id,
                            {"escalation_ids": opened})
        return {"opened": len(opened), "escalation_ids": opened, "swept_at": now}

    # ------------------------------------------------------------- 查询看板

    def _current_owner_map(self, asset_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        if not asset_ids:
            return {}
        result: dict[str, sqlite3.Row] = {}
        for asset_id in asset_ids:
            row = self.connection.execute(
                "SELECT * FROM ownership_versions WHERE asset_id=? ORDER BY version DESC LIMIT 1",
                (asset_id,),
            ).fetchone()
            if row is not None:
                result[asset_id] = row
        return result

    def dashboard(self, actor_id: str, recall_id: str) -> dict[str, Any]:
        """召回负责人视图：范围变化、责任方、逾期与仍在运行的受影响容量。"""

        self._require(actor_id, "report.read")
        recall = self._recall(recall_id)
        current = self._latest_scope_row(recall_id)
        if current is None:
            raise InvalidState("召回尚未初始化范围")
        entries = self._scope_entry_map(recall_id, int(current["scope_version"]))
        included = [a for a, e in entries.items() if e["include_state"] == "included"]
        actions = {
            row["asset_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM asset_actions WHERE recall_id=? AND in_scope=1", (recall_id,)
            ).fetchall()
        }
        assets = {
            row["asset_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM recall_assets WHERE asset_id IN ({})".format(
                    ",".join("?" for _ in included) or "SELECT ''"
                ),
                included,
            ).fetchall()
        } if included else {}
        owners = self._current_owner_map(included)
        now = self._now()

        affected_capacity = Decimal("0")
        running_capacity = Decimal("0")
        holders: dict[str, dict[str, Any]] = {}
        overdue: list[dict[str, Any]] = []
        for asset_id in included:
            asset = assets.get(asset_id)
            capacity = Decimal("0") if asset is None else Decimal(asset["capacity_kwh"])
            affected_capacity += capacity
            action = actions.get(asset_id)
            quarantined = self.connection.execute(
                "SELECT 1 FROM action_stage_completions WHERE recall_id=? AND asset_id=? "
                "AND stage='quarantine'",
                (recall_id, asset_id),
            ).fetchone()
            if asset is not None and asset["running"] and quarantined is None:
                running_capacity += capacity
            owner = owners.get(asset_id)
            holder_key = None if owner is None else owner["holder_id"]
            if holder_key is not None:
                bucket = holders.setdefault(holder_key, {"asset_count": 0, "in_transfer": 0})
                bucket["asset_count"] += 1
                if owner["mode"] == "in_transfer":
                    bucket["in_transfer"] += 1
            if action is not None and action["current_due_at"] is not None and action["current_due_at"] < now:
                open_escalation = self.connection.execute(
                    "SELECT reason FROM escalations WHERE recall_id=? AND asset_id=? AND status='open'",
                    (recall_id, asset_id),
                ).fetchone()
                overdue.append({
                    "asset_id": asset_id,
                    "stage": action["current_stage"],
                    "due_at": action["current_due_at"],
                    "holder_id": holder_key,
                    "escalation": None if open_escalation is None else open_escalation["reason"],
                })

        stage_counts = {
            row["stage"]: int(row["completed"])
            for row in self.connection.execute(
                "SELECT c.stage AS stage, count(*) AS completed FROM action_stage_completions c "
                "JOIN asset_actions a ON a.recall_id=c.recall_id AND a.asset_id=c.asset_id "
                "WHERE c.recall_id=? AND a.in_scope=1 GROUP BY c.stage",
                (recall_id,),
            ).fetchall()
        }
        open_escalations = self.connection.execute(
            "SELECT count(*) FROM escalations WHERE recall_id=? AND status='open'", (recall_id,)
        ).fetchone()[0]

        versions = []
        for row in self.connection.execute(
            "SELECT v.scope_version,v.change_kind,v.status,v.reason,v.genealogy_revision,v.as_of,"
            "v.basis_sha256,v.content_sha256,v.parent_scope_version,v.created_by,v.created_at,"
            "v.decided_by,v.decided_at,"
            "sum(CASE WHEN e.include_state='included' THEN 1 ELSE 0 END) AS included_count,"
            "sum(CASE WHEN e.include_state='excluded' THEN 1 ELSE 0 END) AS excluded_count "
            "FROM scope_versions v LEFT JOIN scope_entries e "
            "ON e.recall_id=v.recall_id AND e.scope_version=v.scope_version "
            "WHERE v.recall_id=? GROUP BY v.scope_version ORDER BY v.scope_version",
            (recall_id,),
        ).fetchall():
            versions.append(dict(row))

        return {
            "recall_id": recall_id,
            "state": recall["state"],
            "component_lot_id": recall["component_lot_id"],
            "current_scope_version": int(current["scope_version"]),
            "generated_at": now,
            "scope": {
                "included_assets": len(included),
                "excluded_assets": sum(1 for e in entries.values() if e["include_state"] == "excluded"),
            },
            "capacity_kwh": {
                "affected": quantize_capacity(affected_capacity),
                "running_affected": quantize_capacity(running_capacity),
            },
            "current_holders": [
                {"holder_id": holder_id, **counts}
                for holder_id, counts in sorted(holders.items())
            ],
            "assets_without_owner": [a for a in included if a not in owners],
            "overdue_actions": sorted(overdue, key=lambda item: item["due_at"]),
            "stage_completions": {stage: stage_counts.get(stage, 0) for stage in ACTION_STAGES},
            "open_escalations": int(open_escalations),
            "versions": versions,
        }

    def asset_detail(self, actor_id: str, recall_id: str, asset_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        action = self.connection.execute(
            "SELECT * FROM asset_actions WHERE recall_id=? AND asset_id=?",
            (recall_id, asset_id),
        ).fetchone()
        if action is None:
            raise NotFound("该资产不在召回范围")
        receipts = self.connection.execute(
            "SELECT receipt_id,stage,state,holder_id,owner_version,evidence_sha256,note,"
            "recorded_by,recorded_at FROM action_receipts WHERE recall_id=? AND asset_id=? "
            "ORDER BY receipt_id",
            (recall_id, asset_id),
        ).fetchall()
        completions = self.connection.execute(
            "SELECT stage,due_at,completed_at,holder_id,owner_version FROM action_stage_completions "
            "WHERE recall_id=? AND asset_id=? ORDER BY completed_at",
            (recall_id, asset_id),
        ).fetchall()
        return {"action": dict(action),
                "receipts": [dict(row) for row in receipts],
                "completions": [dict(row) for row in completions]}

    # ------------------------------------------------------------- 审计复算

    def scope_version(self, actor_id: str, recall_id: str, scope_version: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM scope_versions WHERE recall_id=? AND scope_version=?",
            (recall_id, scope_version),
        ).fetchone()
        if row is None:
            raise NotFound("范围版本不存在")
        entries = self.connection.execute(
            "SELECT * FROM scope_entries WHERE recall_id=? AND scope_version=? ORDER BY asset_id",
            (recall_id, scope_version),
        ).fetchall()
        return {
            "version": {k: row[k] for k in row.keys()},
            "entries": [
                {"asset_id": item["asset_id"], "include_state": item["include_state"],
                 "added_in_version": item["added_in_version"],
                 "match_path": json.loads(item["match_path_json"]),
                 "first_noticed_at": item["first_noticed_at"]}
                for item in entries
            ],
        }

    def recompute_scope(self, actor_id: str, recall_id: str, scope_version: int) -> dict[str, Any]:
        """从冻结谱系与所有权版本独立复算任一范围版本。"""

        self._require(actor_id, "audit.read")
        recall = self._recall(recall_id)
        stored = self.connection.execute(
            "SELECT * FROM scope_versions WHERE recall_id=? AND scope_version=?",
            (recall_id, scope_version),
        ).fetchone()
        if stored is None:
            raise NotFound("范围版本不存在")

        def replay(version_row: sqlite3.Row) -> tuple[list[dict[str, Any]], str]:
            parent_content: str | None = None
            parent_map: dict[str, dict[str, Any]] = {}
            if version_row["parent_scope_version"] is not None:
                parent_row = self.connection.execute(
                    "SELECT * FROM scope_versions WHERE recall_id=? AND scope_version=?",
                    (recall_id, version_row["parent_scope_version"]),
                ).fetchone()
                parent_entries, parent_content = replay(parent_row)
                parent_map = {entry["asset_id"]: entry for entry in parent_entries}
            if version_row["change_kind"] == "reduce":
                excluded_rows = self.connection.execute(
                    "SELECT asset_id FROM scope_entries WHERE recall_id=? AND scope_version=? "
                    "AND include_state='excluded'",
                    (recall_id, version_row["scope_version"]),
                ).fetchall()
                excluded = frozenset(row["asset_id"] for row in excluded_rows)
                edges: list[Edge] = []
            else:
                excluded = None
                edges = self._edges_at(int(version_row["genealogy_revision"]))
            entries = build_entries(
                change_kind=version_row["change_kind"],
                seed=recall["component_lot_id"],
                edges=edges,
                parents=parent_map,
                excluded=excluded,
                scope_version=int(version_row["scope_version"]),
            )
            content = scope_content(
                recall_id=recall_id,
                scope_version=int(version_row["scope_version"]),
                change_kind=version_row["change_kind"],
                genealogy_revision=int(version_row["genealogy_revision"]),
                as_of=version_row["as_of"],
                entries=entries,
            )
            return entries, digest_value(content)

        entries, recomputed_sha = replay(stored)
        matches = recomputed_sha == stored["content_sha256"]
        return {
            "recall_id": recall_id,
            "scope_version": scope_version,
            "stored_content_sha256": stored["content_sha256"],
            "recomputed_content_sha256": recomputed_sha,
            "content_matches": matches,
            "stored_basis_sha256": stored["basis_sha256"],
            "recomputed_entries": entries,
            "genealogy_revision": int(stored["genealogy_revision"]),
            "as_of": stored["as_of"],
        }

    def recall_history(self, actor_id: str, recall_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        self._recall(recall_id)
        rows = self.connection.execute(
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at,"
            "previous_hash,event_hash FROM recall_audit_events "
            "WHERE entity_id=? OR entity_id LIKE ? ORDER BY event_id",
            (recall_id, f"{recall_id}/%"),
        ).fetchall()
        return {"recall_id": recall_id, "events": [
            dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows
        ]}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM recall_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
