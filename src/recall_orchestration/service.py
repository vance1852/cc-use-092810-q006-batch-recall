"""批次召回编排的领域用例。

范围版本从冻结谱系与有效所有权版本计算；每资产的措施状态单调推进；
所有权转移保留召回约束；范围缩减需要独立批准；无法联系的持有人进入升级队列。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .lineage import (
    AssetNode,
    FrozenLineage,
    LineageEdge,
    LineageError,
    OwnershipVersion,
)
from .models import (
    KINDS,
    OWNERSHIP_KINDS,
    decimal_value,
    identifier,
    required_text,
    timestamp,
)
from .scope import (
    ScopeRequest,
    canonical_json,
    compute_scope,
    decimal_text,
    digest,
    quantize_kwh,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "recall_lead": {
        "notice.write", "lineage.write", "scope.write", "scope.shrink",
        "receipt.notify", "escalation.write", "report.read",
    },
    "field_agent": {
        "receipt.notify", "receipt.write", "escalation.write",
        "escalation.resolve", "report.read",
    },
    "approver": {"scope.approve", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

EXPANSION_DIRECTIONS = {"downstream", "upstream", "both"}
PHYSICAL_ORDER = {
    "pending": 0,
    "quarantined": 1,
    "inspected": 2,
    "returned": 3,
    "released": 4,
}
PHYSICAL_TARGET = {
    "quarantine": "quarantined",
    "inspect": "inspected",
    "return_to_factory": "returned",
    "release": "released",
}
PHYSICAL_REQUIRED = {
    "quarantine": "pending",
    "inspect": "quarantined",
    "return_to_factory": "inspected",
}
RELEASE_FROM = {"inspected", "returned"}
SLA_REASONS = {
    "acknowledge": ("ack_within_hours", "no_acknowledgement"),
    "quarantine": ("quarantine_within_hours", "action_overdue"),
    "inspect": ("inspect_within_hours", "action_overdue"),
    "return_to_factory": ("return_within_hours", "action_overdue"),
}


class RecallOrchestrationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ── 账户与权限 ─────────────────────────────────────────────

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

    # ── 风险通知 ───────────────────────────────────────────────

    def create_notice(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "notice.write")
        notice_id = identifier(raw.get("notice_id"), "notice_id")
        supplier_batch_id = identifier(raw.get("supplier_batch_id"), "supplier_batch_id")
        title = required_text(raw.get("title"), "title")
        risk_description = required_text(raw.get("risk_description"), "risk_description", 2000)
        supplier_ref = required_text(raw.get("supplier_ref"), "supplier_ref")
        issued_at = timestamp(raw.get("issued_at", self._now()), "issued_at")
        slas = {
            "ack_within_hours": int(raw.get("ack_within_hours", 72)),
            "quarantine_within_hours": int(raw.get("quarantine_within_hours", 168)),
            "inspect_within_hours": int(raw.get("inspect_within_hours", 336)),
            "return_within_hours": int(raw.get("return_within_hours", 720)),
        }
        for key, value in slas.items():
            if isinstance(value, bool) or value <= 0:
                raise ValidationFailed(f"{key} 必须是正整数")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO notices(notice_id,supplier_batch_id,title,risk_description,supplier_ref,"
                    "ack_within_hours,quarantine_within_hours,inspect_within_hours,return_within_hours,"
                    "issued_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        notice_id, supplier_batch_id, title, risk_description, supplier_ref,
                        slas["ack_within_hours"], slas["quarantine_within_hours"],
                        slas["inspect_within_hours"], slas["return_within_hours"],
                        issued_at, actor_id, self._now(),
                    ),
                )
                self._audit("notice", notice_id, "notice.issued", actor_id, {
                    "supplier_batch_id": supplier_batch_id,
                    "supplier_ref": supplier_ref,
                    "issued_at": issued_at,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("召回通知编号已经存在") from exc
        return {"notice_id": notice_id, "supplier_batch_id": supplier_batch_id, "state": "open"}

    def _notice(self, notice_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM notices WHERE notice_id=?", (notice_id,)).fetchone()
        if row is None:
            raise NotFound("召回通知不存在")
        return row

    # ── 冻结谱系证据（只增） ───────────────────────────────────

    def record_lineage_revision(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记一批只增的谱系证据：新资产、组装/装入/拆分边、所有权版本。

        修订一经登记即冻结：指纹覆盖修订正文，previous 指针形成证据链，
        相同正文不可重复登记。
        """

        self._require(actor_id, "lineage.write")
        revision_id = identifier(raw.get("revision_id"), "revision_id")
        notice_id = raw.get("notice_id")
        if notice_id is not None:
            notice_id = identifier(notice_id, "notice_id")
            self._notice(notice_id)
        body = {
            "assets": self._parse_assets(raw.get("assets", [])),
            "edges": self._parse_edges(raw.get("edges", [])),
            "ownership": self._parse_ownership(raw.get("ownership", [])),
        }
        if not body["assets"] and not body["edges"] and not body["ownership"]:
            raise ValidationFailed("谱系修订至少要包含资产、谱系边或所有权版本之一")
        previous_id = raw.get("previous_revision_id")
        previous_id = None if previous_id in (None, "") else identifier(previous_id, "previous_revision_id")
        content_sha256 = digest(body)
        if self.connection.execute(
            "SELECT 1 FROM lineage_revisions WHERE content_sha256=?", (content_sha256,)
        ).fetchone():
            raise Conflict("相同内容的谱系修订已经登记，不能重复冻结")
        previous_hash = None
        if previous_id is not None:
            previous = self.connection.execute(
                "SELECT content_sha256,frozen_at FROM lineage_revisions WHERE revision_id=?",
                (previous_id,),
            ).fetchone()
            if previous is None:
                raise ValidationFailed("previous_revision_id 不存在")
            if previous["frozen_at"] is None:
                raise InvalidState("前序谱系修订尚未冻结")
            previous_hash = previous["content_sha256"]
        now = self._now()
        affected_assets = {item["asset_id"] for item in body["ownership"]}
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO lineage_revisions(revision_id,notice_id,content_sha256,previous_revision_id,"
                    "previous_content_sha256,frozen_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (revision_id, notice_id, content_sha256, previous_id, previous_hash, now, actor_id, now),
                )
                for asset in body["assets"]:
                    try:
                        self.connection.execute(
                            "INSERT INTO assets(asset_id,asset_kind,capacity_kwh,top_level,"
                            "first_seen_revision_id,created_at) VALUES(?,?,?,?,?,?)",
                            (
                                asset["asset_id"], asset["asset_kind"],
                                decimal_text(asset["capacity_kwh"]),
                                1 if asset["top_level"] else 0,
                                revision_id, now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise Conflict(f"资产已经登记: {asset['asset_id']}") from exc
                known = {
                    row["asset_id"]
                    for row in self.connection.execute("SELECT asset_id FROM assets").fetchall()
                }
                for edge in body["edges"]:
                    if edge["parent_id"] not in known or edge["child_id"] not in known:
                        raise ValidationFailed(f"谱系边 {edge['edge_id']} 引用了未登记资产")
                    try:
                        self.connection.execute(
                            "INSERT INTO lineage_edges(edge_id,revision_id,parent_id,child_id,"
                            "relation,effective_from,created_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                edge["edge_id"], revision_id, edge["parent_id"], edge["child_id"],
                                edge["relation"], edge["effective_from"], now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise Conflict(f"谱系边编号已经存在: {edge['edge_id']}") from exc
                for ownership in body["ownership"]:
                    asset_id = ownership["asset_id"]
                    if asset_id not in known:
                        raise ValidationFailed(f"所有权版本引用了未登记资产: {asset_id}")
                    latest = self.connection.execute(
                        "SELECT max(version) AS version FROM ownership_versions WHERE asset_id=?",
                        (asset_id,),
                    ).fetchone()["version"]
                    expected = 1 if latest is None else latest + 1
                    if ownership["version"] != expected:
                        raise Conflict(
                            f"资产 {asset_id} 的所有权版本必须连续递增，下一个版本为 {expected}"
                        )
                    if ownership["version"] == 1 and ownership["kind"] != "initial":
                        raise ValidationFailed(f"资产 {asset_id} 的首个所有权版本必须是 initial")
                    if ownership["version"] > 1 and ownership["kind"] not in OWNERSHIP_KINDS:
                        raise ValidationFailed(f"资产 {asset_id} 的转移类型不受支持")
                    try:
                        self.connection.execute(
                            "INSERT INTO ownership_versions(asset_id,version,revision_id,holder_id,"
                            "kind,effective_from,created_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                asset_id, ownership["version"], revision_id, ownership["holder_id"],
                                ownership["kind"], ownership["effective_from"], now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise Conflict(f"所有权版本冲突: {asset_id} v{ownership['version']}") from exc
                    # 召回约束随所有权转移：物理处置阶段不回退，只更新当前责任方，
                    # 新持有人需要在下一轮通知中被覆盖；已通知过的资产进入转移待通知升级。
                    affected_rows = self.connection.execute(
                        "SELECT notice_id,asset_id,notified_round FROM asset_actions "
                        "WHERE asset_id=? AND in_scope=1",
                        (asset_id,),
                    ).fetchall()
                    for affected in affected_rows:
                        self.connection.execute(
                            "UPDATE asset_actions SET current_holder_id=?,latest_at=? "
                            "WHERE notice_id=? AND asset_id=? AND in_scope=1",
                            (ownership["holder_id"], now, affected["notice_id"], asset_id),
                        )
                        if int(affected["notified_round"]) > 0:
                            self.connection.execute(
                                "INSERT INTO escalations(notice_id,asset_id,holder_id,round_no,"
                                "reason_code,detail,state,opened_at) "
                                "SELECT ?,?,?,?, 'transfer_pending_notify',?,'open',? "
                                "WHERE NOT EXISTS (SELECT 1 FROM escalations WHERE notice_id=? "
                                "AND asset_id=? AND reason_code='transfer_pending_notify' AND state='open')",
                                (
                                    affected["notice_id"], asset_id, ownership["holder_id"],
                                    int(affected["notified_round"]),
                                    f"所有权经 {ownership['kind']} 转移给 {ownership['holder_id']}，新持有人待通知",
                                    now, affected["notice_id"], asset_id,
                                ),
                            )
                            # 连续转移时，开放中的待通知升级改挂最新持有人。
                            self.connection.execute(
                                "UPDATE escalations SET holder_id=?,detail=? "
                                "WHERE notice_id=? AND asset_id=? "
                                "AND reason_code='transfer_pending_notify' AND state='open'",
                                (
                                    ownership["holder_id"],
                                    f"所有权经 {ownership['kind']} 转移给 {ownership['holder_id']}，新持有人待通知",
                                    affected["notice_id"], asset_id,
                                ),
                            )
                self._audit("lineage_revision", revision_id, "lineage.revision_frozen", actor_id, {
                    "notice_id": notice_id,
                    "previous_revision_id": previous_id,
                    "content_sha256": content_sha256,
                    "assets": len(body["assets"]),
                    "edges": len(body["edges"]),
                    "ownership": len(body["ownership"]),
                    "transferred_assets": sorted(affected_assets),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("谱系修订编号冲突") from exc
        return {
            "revision_id": revision_id,
            "content_sha256": content_sha256,
            "frozen": True,
            "assets": len(body["assets"]),
            "edges": len(body["edges"]),
            "ownership": len(body["ownership"]),
        }

    @staticmethod
    def _parse_assets(raw: object) -> list[dict[str, Any]]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValidationFailed("assets 必须是数组")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("assets 条目必须是对象")
            asset_id = identifier(item.get("asset_id"), "assets.asset_id")
            if asset_id in seen:
                raise ValidationFailed(f"资产在修订内重复: {asset_id}")
            seen.add(asset_id)
            result.append({
                "asset_id": asset_id,
                "asset_kind": required_text(item.get("asset_kind"), "assets.asset_kind", 48),
                "capacity_kwh": decimal_value(item.get("capacity_kwh"), "assets.capacity_kwh", minimum=Decimal("0")),
                "top_level": bool(item.get("top_level", False)),
            })
        return result

    @staticmethod
    def _parse_edges(raw: object) -> list[dict[str, Any]]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValidationFailed("edges 必须是数组")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("edges 条目必须是对象")
            edge_id = identifier(item.get("edge_id"), "edges.edge_id")
            if edge_id in seen:
                raise ValidationFailed(f"谱系边在修订内重复: {edge_id}")
            seen.add(edge_id)
            relation = required_text(item.get("relation"), "edges.relation", 32)
            if relation not in KINDS:
                raise ValidationFailed("edges.relation 不受支持")
            parent_id = identifier(item.get("parent_id"), "edges.parent_id")
            child_id = identifier(item.get("child_id"), "edges.child_id")
            if parent_id == child_id:
                raise ValidationFailed(f"谱系边 {edge_id} 的两端不能相同")
            result.append({
                "edge_id": edge_id,
                "parent_id": parent_id,
                "child_id": child_id,
                "relation": relation,
                "effective_from": timestamp(item.get("effective_from"), "edges.effective_from"),
            })
        return result

    @staticmethod
    def _parse_ownership(raw: object) -> list[dict[str, Any]]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValidationFailed("ownership 必须是数组")
        result: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("ownership 条目必须是对象")
            asset_id = identifier(item.get("asset_id"), "ownership.asset_id")
            version = item.get("version")
            if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
                raise ValidationFailed("ownership.version 必须是正整数")
            kind = required_text(item.get("kind"), "ownership.kind", 32)
            if kind not in OWNERSHIP_KINDS | {"initial"}:
                raise ValidationFailed("ownership.kind 不受支持")
            result.append({
                "asset_id": asset_id,
                "version": version,
                "holder_id": identifier(item.get("holder_id"), "ownership.holder_id"),
                "kind": kind,
                "effective_from": timestamp(item.get("effective_from"), "ownership.effective_from"),
            })
        return result

    # ── 范围版本：从冻结快照计算 ───────────────────────────────

    def _lineage_snapshot(self, as_of: str) -> dict[str, Any]:
        """取业务时间 as_of 的谱系切面。

        谱系证据只增不改，因此全部已冻结修订可见；边与所有权版本再按各自
        effective_from 裁剪到业务时间 as_of（修订可以补记过去发生的事实）。
        """

        revisions = self.connection.execute(
            "SELECT revision_id FROM lineage_revisions WHERE frozen_at IS NOT NULL ORDER BY revision_id"
        ).fetchall()
        revision_ids = [row["revision_id"] for row in revisions]
        nodes = [
            {
                "asset_id": row["asset_id"],
                "asset_kind": row["asset_kind"],
                "capacity_kwh": row["capacity_kwh"],
                "top_level": bool(row["top_level"]),
            }
            for row in self.connection.execute(
                "SELECT asset_id,asset_kind,capacity_kwh,top_level FROM assets ORDER BY asset_id"
            ).fetchall()
        ]
        edges = [
            {
                "edge_id": row["edge_id"],
                "parent_id": row["parent_id"],
                "child_id": row["child_id"],
                "relation": row["relation"],
                "effective_from": row["effective_from"],
            }
            for row in self.connection.execute(
                "SELECT * FROM lineage_edges WHERE effective_from<=? ORDER BY edge_id",
                (as_of,),
            ).fetchall()
        ]
        ownership = [
            {
                "asset_id": row["asset_id"],
                "version": row["version"],
                "holder_id": row["holder_id"],
                "kind": row["kind"],
                "effective_from": row["effective_from"],
            }
            for row in self.connection.execute(
                "SELECT * FROM ownership_versions WHERE effective_from<=? "
                "ORDER BY asset_id,version",
                (as_of,),
            ).fetchall()
        ]
        return {
            "revision_ids": revision_ids,
            "nodes": nodes,
            "edges": edges,
            "ownership": ownership,
        }

    @staticmethod
    def _freeze_lineage(snapshot: Mapping[str, Any]) -> FrozenLineage:
        nodes = {
            item["asset_id"]: AssetNode(
                asset_id=item["asset_id"],
                kind=item["asset_kind"],
                capacity_kwh=Decimal(str(item["capacity_kwh"])),
                top_level=bool(item["top_level"]),
            )
            for item in snapshot["nodes"]
        }
        edges = tuple(
            LineageEdge(
                edge_id=item["edge_id"],
                parent_id=item["parent_id"],
                child_id=item["child_id"],
                relation=item["relation"],
                effective_from=item["effective_from"],
            )
            for item in snapshot["edges"]
        )
        ownership: dict[str, list[OwnershipVersion]] = {}
        for item in snapshot["ownership"]:
            ownership.setdefault(item["asset_id"], []).append(
                OwnershipVersion(
                    asset_id=item["asset_id"],
                    version=int(item["version"]),
                    holder_id=item["holder_id"],
                    kind=item["kind"],
                    effective_from=item["effective_from"],
                )
            )
        return FrozenLineage(nodes=nodes, edges=edges, ownership=ownership)

    def _active_version(self, notice_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM scope_versions WHERE notice_id=? AND state='active' "
            "ORDER BY version_no DESC LIMIT 1",
            (notice_id,),
        ).fetchone()

    def _member_set(self, scope_version_id: int) -> dict[str, sqlite3.Row]:
        return {
            row["asset_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM scope_version_members WHERE scope_version_id=?",
                (scope_version_id,),
            ).fetchall()
        }

    def _active_member_ids(self, notice_id: str) -> list[str]:
        active = self._active_version(notice_id)
        if active is None:
            return []
        return [
            row["asset_id"]
            for row in self.connection.execute(
                "SELECT asset_id FROM scope_version_members WHERE scope_version_id=? ORDER BY asset_id",
                (active["scope_version_id"],),
            ).fetchall()
        ]

    def _store_scope_version(
        self,
        *,
        notice: sqlite3.Row,
        direction: str,
        spread_direction: str,
        reason: str,
        as_of: str,
        seeds: Sequence[str],
        base_member_ids: Sequence[str],
        state: str,
        created_by: str,
    ) -> dict[str, Any]:
        snapshot = self._lineage_snapshot(as_of)
        lineage = self._freeze_lineage(snapshot)
        request = ScopeRequest(
            notice_id=notice["notice_id"],
            seeds=seeds,
            direction=spread_direction,
            as_of=as_of,
            reason=reason,
            lineage_revision_ids=snapshot["revision_ids"],
            base_member_ids=base_member_ids,
        )
        result = compute_scope(lineage, request)
        previous = self._active_version(notice["notice_id"])
        previous_members = {} if previous is None else self._member_set(previous["scope_version_id"])
        current_ids = {item["asset_id"] for item in result["members"]}
        previous_ids = set(previous_members)
        change_summary = {
            "added": sorted(current_ids - previous_ids),
            "removed": sorted(previous_ids - current_ids),
            "from_version": None if previous is None else previous["version_no"],
        }
        version_no = self.connection.execute(
            "SELECT coalesce(max(version_no),0)+1 AS next FROM scope_versions WHERE notice_id=?",
            (notice["notice_id"],),
        ).fetchone()["next"]
        now = self._now()
        cursor = self.connection.execute(
            "INSERT INTO scope_versions(notice_id,version_no,direction,spread_direction,reason,as_of,seeds_json,"
            "base_member_ids_json,lineage_snapshot_json,lineage_revision_ids_json,input_sha256,output_sha256,"
            "affected_assets,affected_top_level_capacity_kwh,holder_counts_json,change_summary_json,"
            "state,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                notice["notice_id"], version_no, direction, spread_direction, reason, as_of,
                canonical_json(sorted(seeds)),
                canonical_json(sorted(base_member_ids)),
                canonical_json(snapshot),
                canonical_json(snapshot["revision_ids"]),
                result["input_sha256"], result["output_sha256"],
                result["affected_assets"], result["affected_top_level_capacity_kwh"],
                canonical_json(result["holder_counts"]),
                canonical_json(change_summary),
                state, created_by, now,
            ),
        )
        scope_version_id = int(cursor.lastrowid)
        self.connection.executemany(
            "INSERT INTO scope_version_members(scope_version_id,asset_id,asset_kind,holder_id,"
            "ownership_kind,capacity_kwh,top_level) VALUES(?,?,?,?,?,?,?)",
            [
                (
                    scope_version_id,
                    item["asset_id"],
                    item["asset_kind"],
                    item["holder_id"],
                    item["ownership_kind"],
                    item["capacity_kwh"],
                    1 if item["top_level"] else 0,
                )
                for item in result["members"]
            ],
        )
        return {
            "scope_version_id": scope_version_id,
            "version_no": version_no,
            "direction": direction,
            "spread_direction": spread_direction,
            "state": state,
            "reason": reason,
            "as_of": as_of,
            "seeds": sorted(seeds),
            "input_sha256": result["input_sha256"],
            "output_sha256": result["output_sha256"],
            "affected_assets": result["affected_assets"],
            "affected_top_level_capacity_kwh": result["affected_top_level_capacity_kwh"],
            "holder_counts": result["holder_counts"],
            "change_summary": change_summary,
            "members": result["members"],
            "base_member_ids": sorted(base_member_ids),
            "lineage_revision_ids": snapshot["revision_ids"],
            "created_by": created_by,
            "created_at": now,
        }

    def compute_initial_scope(self, actor_id: str, notice_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """从最初收货批次与冻结谱系计算初始召回范围，直接生效。"""

        self._require(actor_id, "scope.write")
        notice = self._notice(notice_id)
        existing = self.connection.execute(
            "SELECT count(*) AS n FROM scope_versions WHERE notice_id=?", (notice_id,)
        ).fetchone()["n"]
        if existing:
            raise InvalidState("初始范围已经存在，新证据请提交范围修订提案")
        seeds = self._parse_seeds(raw, default=[notice["supplier_batch_id"]])
        direction = required_text(raw.get("direction", "downstream"), "direction", 16)
        if direction not in EXPANSION_DIRECTIONS:
            raise ValidationFailed("初始范围方向必须是 downstream、upstream 或 both")
        reason = required_text(raw.get("reason", f"供应商批次 {notice['supplier_batch_id']} 风险通知初始范围"), "reason", 500)
        as_of = timestamp(raw.get("as_of", self._now()), "as_of")
        try:
            with transaction(self.connection, immediate=True):
                record = self._store_scope_version(
                    notice=notice, direction="initial", spread_direction=direction, reason=reason,
                    as_of=as_of, seeds=seeds, base_member_ids=(),
                    state="active", created_by=actor_id,
                )
                self._sync_actions_on_activation(record["scope_version_id"], record["change_summary"])
                self._audit("notice", notice_id, "scope.computed", actor_id, {
                    "version_no": 1,
                    "direction": direction,
                    "reason": reason,
                    "as_of": as_of,
                    "seeds": sorted(seeds),
                    "affected_assets": record["affected_assets"],
                    "affected_top_level_capacity_kwh": record["affected_top_level_capacity_kwh"],
                    "input_sha256": record["input_sha256"],
                    "output_sha256": record["output_sha256"],
                })
        except LineageError as exc:
            raise ValidationFailed(str(exc)) from exc
        return record

    def propose_scope_revision(self, actor_id: str, notice_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """新的谱系证据表明风险扩散时，生成待批准的新范围版本提案。"""

        self._require(actor_id, "scope.write")
        notice = self._notice(notice_id)
        if self._active_version(notice_id) is None:
            raise InvalidState("请先计算初始范围")
        direction = required_text(raw.get("direction", "downstream"), "direction", 16)
        if direction not in EXPANSION_DIRECTIONS:
            raise ValidationFailed("扩散方向必须是 downstream、upstream 或 both")
        reason = required_text(raw.get("reason"), "reason", 500)
        as_of = timestamp(raw.get("as_of", self._now()), "as_of")
        seeds = self._parse_seeds(
            raw,
            default=self._current_seeds(notice_id),
        )
        self._require_no_pending_proposal(notice_id)
        try:
            with transaction(self.connection, immediate=True):
                record = self._store_scope_version(
                    notice=notice, direction=direction, spread_direction=direction, reason=reason,
                    as_of=as_of, seeds=seeds,
                    base_member_ids=self._active_member_ids(notice_id),
                    state="proposed", created_by=actor_id,
                )
                if not record["change_summary"]["added"]:
                    raise InvalidState("新谱系证据没有向当前范围增加任何资产，不构成扩散")
                self._audit("notice", notice_id, "scope.proposed", actor_id, {
                    "version_no": record["version_no"],
                    "direction": direction,
                    "reason": reason,
                    "added": record["change_summary"]["added"],
                    "removed": record["change_summary"]["removed"],
                    "input_sha256": record["input_sha256"],
                    "output_sha256": record["output_sha256"],
                })
        except LineageError as exc:
            raise ValidationFailed(str(exc)) from exc
        return record

    def propose_shrink(self, actor_id: str, notice_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """范围缩减必须独立提出：记录新种子与原因，保留全部历史版本待批准。"""

        self._require(actor_id, "scope.shrink")
        notice = self._notice(notice_id)
        if self._active_version(notice_id) is None:
            raise InvalidState("初始范围尚未计算")
        reason = required_text(raw.get("reason"), "reason", 500)
        as_of = timestamp(raw.get("as_of", self._now()), "as_of")
        direction = required_text(raw.get("direction", "downstream"), "direction", 16)
        if direction not in EXPANSION_DIRECTIONS:
            raise ValidationFailed("缩减后的扩散方向必须是 downstream、upstream 或 both")
        seeds = self._parse_seeds(raw, default=[])
        if not seeds:
            raise ValidationFailed("缩减提案必须显式给出保留的批次种子")
        self._require_no_pending_proposal(notice_id)
        try:
            with transaction(self.connection, immediate=True):
                record = self._store_scope_version(
                    notice=notice, direction="shrink", spread_direction=direction, reason=reason,
                    as_of=as_of, seeds=seeds, base_member_ids=(),
                    state="proposed", created_by=actor_id,
                )
                if not record["change_summary"]["removed"]:
                    raise InvalidState("新范围没有移除任何资产，不构成缩减")
                self._audit("notice", notice_id, "scope.shrink_proposed", actor_id, {
                    "version_no": record["version_no"],
                    "reason": reason,
                    "seeds": sorted(seeds),
                    "removed": record["change_summary"]["removed"],
                    "input_sha256": record["input_sha256"],
                    "output_sha256": record["output_sha256"],
                })
        except LineageError as exc:
            raise ValidationFailed(str(exc)) from exc
        return record

    def _require_no_pending_proposal(self, notice_id: str) -> None:
        pending = self.connection.execute(
            "SELECT version_no FROM scope_versions WHERE notice_id=? AND state='proposed' "
            "ORDER BY version_no",
            (notice_id,),
        ).fetchall()
        if pending:
            raise InvalidState(
                "存在尚未决定的范围版本提案：" + ",".join(str(row["version_no"]) for row in pending)
            )

    def decide_scope_revision(
        self, actor_id: str, notice_id: str, version_no: int, approve: bool, note: str
    ) -> dict[str, Any]:
        """独立批准岗位对范围提案作决定；拒绝不改变当前范围，批准才切换活动版本。"""

        self._require(actor_id, "scope.approve")
        note = required_text(note, "note", 500)
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM scope_versions WHERE notice_id=? AND version_no=?",
                (notice_id, version_no),
            ).fetchone()
            if row is None:
                raise NotFound("范围版本不存在")
            if row["state"] != "proposed":
                raise InvalidState("范围版本不是待批准提案")
            now = self._now()
            if not approve:
                self.connection.execute(
                    "UPDATE scope_versions SET state='rejected' WHERE scope_version_id=?",
                    (row["scope_version_id"],),
                )
                self._audit("notice", notice_id, "scope.rejected", actor_id, {
                    "version_no": version_no, "note": note,
                })
                return {"notice_id": notice_id, "version_no": version_no, "state": "rejected"}
            self.connection.execute(
                "UPDATE scope_versions SET state='superseded' WHERE notice_id=? AND state='active'",
                (notice_id,),
            )
            self.connection.execute(
                "UPDATE scope_versions SET state='active',shrink_approved_by=?,shrink_approved_at=? "
                "WHERE scope_version_id=?",
                (actor_id if row["direction"] == "shrink" else None,
                 now if row["direction"] == "shrink" else None,
                 row["scope_version_id"]),
            )
            members = self.connection.execute(
                "SELECT * FROM scope_version_members WHERE scope_version_id=?",
                (row["scope_version_id"],),
            ).fetchall()
            change = json.loads(row["change_summary_json"])
            self._sync_actions_on_activation(row["scope_version_id"], change)
            self._audit("notice", notice_id, "scope.approved", actor_id, {
                "version_no": version_no,
                "direction": row["direction"],
                "note": note,
                "added": change["added"],
                "removed": change["removed"],
                "shrink_approved": row["direction"] == "shrink",
            })
        return self.scope_version(actor_id, notice_id, version_no)

    def _sync_actions_on_activation(self, scope_version_id: int, change: Mapping[str, Any]) -> None:
        """激活新版本：新增成员建档，移出成员保留事实仅置 in_scope=0，留存成员同步责任方。"""

        notice_id = self._notice_of_version(scope_version_id)
        now = self._now()
        members = self.connection.execute(
            "SELECT * FROM scope_version_members WHERE scope_version_id=?",
            (scope_version_id,),
        ).fetchall()
        for member in members:
            existing = self.connection.execute(
                "SELECT action_id FROM asset_actions WHERE notice_id=? AND asset_id=?",
                (notice_id, member["asset_id"]),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO asset_actions(notice_id,asset_id,current_holder_id,physical_stage,"
                    "notified_round,acknowledged_round,first_notified_at,latest_at,in_scope) "
                    "VALUES(?,?,?,'pending',0,0,NULL,?,1)",
                    (notice_id, member["asset_id"], member["holder_id"], now),
                )
            else:
                # 所有权可能已在证据登记时更新；以新版本快照的有效责任方为准，
                # 物理阶段、通知轮次等已完成事实一律不动。
                self.connection.execute(
                    "UPDATE asset_actions SET current_holder_id=?,in_scope=1,latest_at=? "
                    "WHERE notice_id=? AND asset_id=?",
                    (member["holder_id"], now, notice_id, member["asset_id"]),
                )
        for asset_id in change.get("removed", []):
            # 缩减不抹除已通知事实：不删行、不回退阶段，只标记退出当前范围。
            self.connection.execute(
                "UPDATE asset_actions SET in_scope=0,latest_at=? WHERE notice_id=? AND asset_id=?",
                (now, notice_id, asset_id),
            )

    def _notice_of_version(self, scope_version_id: int) -> str:
        return self.connection.execute(
            "SELECT notice_id FROM scope_versions WHERE scope_version_id=?", (scope_version_id,)
        ).fetchone()["notice_id"]

    def _current_seeds(self, notice_id: str) -> list[str]:
        active = self._active_version(notice_id)
        if active is None:
            return []
        return json.loads(active["seeds_json"])

    @staticmethod
    def _parse_seeds(raw: Mapping[str, Any], *, default: Sequence[str]) -> list[str]:
        value = raw.get("seeds", default)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValidationFailed("seeds 必须是数组")
        seeds = [identifier(item, "seeds 条目") for item in value]
        if not seeds:
            raise ValidationFailed("seeds 不能为空")
        if len(set(seeds)) != len(seeds):
            raise ValidationFailed("seeds 不能重复")
        return seeds

    # ── 每资产措施推进（单调、幂等、拒绝乱序） ─────────────────

    def record_receipt(
        self,
        actor_id: str,
        notice_id: str,
        asset_id: str,
        action: str,
        idempotency_key: str,
        *,
        holder_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        permission = "receipt.notify" if action == "notify" else "receipt.write"
        self._require(actor_id, permission)
        self._notice(notice_id)
        if action not in {"notify", "acknowledge", "quarantine", "inspect", "return_to_factory", "release"}:
            raise ValidationFailed("未知措施")
        idempotency_key = identifier(idempotency_key, "idempotency_key")
        note_text = (note or "").strip()
        if len(note_text) > 500:
            raise ValidationFailed("note 不能超过 500 个字符")
        request_digest = digest({
            "notice_id": notice_id, "asset_id": asset_id, "action": action,
            "holder_id": holder_id, "note": note_text,
        })
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM recall_idempotency "
            "WHERE scope='receipt' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同回执内容")
            response = json.loads(stored["response_json"])
            response["idempotent_replay"] = True
            return response
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM asset_actions WHERE notice_id=? AND asset_id=?",
                (notice_id, asset_id),
            ).fetchone()
            if row is None:
                raise NotFound("资产不在该召回的任何范围版本中")
            if not row["in_scope"]:
                raise InvalidState("资产已退出当前召回范围，不能继续登记措施回执")
            current_holder = row["current_holder_id"]
            if holder_id is not None and holder_id != current_holder:
                raise Conflict("回执持有人与资产当前责任方不一致，召回约束随资产归属")
            effective_holder = holder_id or current_holder
            now_text = self._now()
            duplicate = self._existing_receipt(notice_id, asset_id, action, row)
            if duplicate is not None:
                # 重复回执：回放既有结果，不插入新行、不改变任何状态。
                response = self._receipt_response(duplicate, row, duplicate=True)
                self._store_idempotency(idempotency_key, request_digest, response, now_text)
                return response
            round_no = self._validate_receipt_order(row, action)
            try:
                cursor = self.connection.execute(
                    "INSERT INTO action_receipts(notice_id,asset_id,action,round_no,recorded_holder_id,"
                    "idempotency_key,note,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (notice_id, asset_id, action, round_no, effective_holder,
                     idempotency_key, note_text, actor_id, now_text),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("回执与既有记录冲突") from exc
            receipt_id = int(cursor.lastrowid)
            updates = self._apply_receipt(row, action, round_no, now_text)
            self.connection.execute(
                "UPDATE asset_actions SET " + ", ".join(f"{key}=?" for key in updates) +
                " WHERE action_id=?",
                (*updates.values(), row["action_id"]),
            )
            self._auto_resolve_escalations(notice_id, asset_id, action, round_no, now_text)
            self._audit("asset_action", f"{notice_id}:{asset_id}", f"action.{action}", actor_id, {
                "receipt_id": receipt_id,
                "round_no": round_no,
                "holder_id": effective_holder,
                "note": note_text,
            })
            saved = self.connection.execute(
                "SELECT * FROM action_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            action_row = self.connection.execute(
                "SELECT * FROM asset_actions WHERE action_id=?", (row["action_id"],)
            ).fetchone()
            response = self._receipt_response(saved, action_row, duplicate=False)
            self._store_idempotency(idempotency_key, request_digest, response, now_text)
        return response

    def _store_idempotency(
        self, key: str, request_digest: str, response: Mapping[str, Any], now_text: str
    ) -> None:
        self.connection.execute(
            "INSERT INTO recall_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES('receipt',?,?,?,?)",
            (key, request_digest, canonical_json(response), now_text),
        )

    def _existing_receipt(
        self, notice_id: str, asset_id: str, action: str, row: sqlite3.Row
    ) -> sqlite3.Row | None:
        """返回使本次回执成为重复回放的既有回执；没有则 None。"""

        notified = int(row["notified_round"])
        if action == "notify":
            # 同一责任方已经收到本轮通知 → 重复通知。
            if notified > 0 and row["last_notified_holder_id"] == row["current_holder_id"]:
                return self.connection.execute(
                    "SELECT * FROM action_receipts WHERE notice_id=? AND asset_id=? AND action='notify' "
                    "AND round_no=? ORDER BY receipt_id DESC LIMIT 1",
                    (notice_id, asset_id, notified),
                ).fetchone()
            return None
        if action == "acknowledge":
            if notified > 0 and int(row["acknowledged_round"]) >= notified:
                return self.connection.execute(
                    "SELECT * FROM action_receipts WHERE notice_id=? AND asset_id=? AND action='acknowledge' "
                    "AND round_no=? ORDER BY receipt_id DESC LIMIT 1",
                    (notice_id, asset_id, notified),
                ).fetchone()
            return None
        # 物理措施是一次性阶段跃迁：该措施已有回执时，只有资产仍停留在该措施
        # 完成的阶段才作幂等回放；阶段已经前进后迟到的同措施回执属于乱序，
        # 交由顺序校验拒绝，确保已完成状态不被倒退。
        existing = self.connection.execute(
            "SELECT * FROM action_receipts WHERE notice_id=? AND asset_id=? AND action=? "
            "ORDER BY receipt_id DESC LIMIT 1",
            (notice_id, asset_id, action),
        ).fetchone()
        if existing is not None and row["physical_stage"] == PHYSICAL_TARGET[action]:
            return existing
        return None

    def _validate_receipt_order(self, row: sqlite3.Row, action: str) -> int:
        """校验乱序/倒退；返回回执所属通知轮次。"""

        notified = int(row["notified_round"])
        stage = row["physical_stage"]
        if action == "notify":
            if notified > 0 and row["last_notified_holder_id"] == row["current_holder_id"]:
                raise InvalidState("当前责任方已收到本轮通知")
            return notified + 1
        if notified == 0:
            raise InvalidState("资产尚未通知，不能登记后续措施回执")
        if action == "acknowledge":
            if row["current_holder_id"] is None:
                raise InvalidState("当前责任方未知，无法签收，请先走无法联系升级")
            if int(row["acknowledged_round"]) >= notified:
                raise InvalidState("当前轮次已经签收")
            return notified
        # 物理措施只能由已收到本轮通知的当前责任方推进，避免转移后旧持有人补录。
        if row["last_notified_holder_id"] != row["current_holder_id"]:
            raise InvalidState("资产所有权已变更，必须先向当前责任方发出新一轮通知")
        if action == "release":
            if stage == "released":
                raise InvalidState("资产已经解除，措施不得重复")
            if stage not in RELEASE_FROM:
                raise InvalidState("解除前必须先完成现场检查（或返厂）")
            return notified
        required_stage = PHYSICAL_REQUIRED[action]
        if PHYSICAL_ORDER[stage] > PHYSICAL_ORDER[required_stage]:
            raise InvalidState(f"措施乱序：资产已处于 {stage}，不能回退登记 {action}")
        if stage != required_stage:
            raise InvalidState(f"措施乱序：{action} 要求先完成 {required_stage}")
        return notified

    def _apply_receipt(
        self, row: sqlite3.Row, action: str, round_no: int, now_text: str
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"latest_at": now_text}
        if action == "notify":
            updates["notified_round"] = round_no
            updates["last_notified_holder_id"] = row["current_holder_id"]
            if row["first_notified_at"] is None:
                updates["first_notified_at"] = now_text
        elif action == "acknowledge":
            if int(row["acknowledged_round"]) < round_no:
                updates["acknowledged_round"] = round_no
        else:
            updates["physical_stage"] = PHYSICAL_TARGET[action]
        return updates

    def _auto_resolve_escalations(
        self, notice_id: str, asset_id: str, action: str, round_no: int, now_text: str
    ) -> None:
        reasons: list[str] = []
        if action == "notify":
            # 新一轮通知发出：转移待通知与无法联系状态随之解除。
            reasons = ["transfer_pending_notify", "unreachable"]
        elif action == "acknowledge":
            reasons = ["no_acknowledgement", "unreachable", "action_overdue"]
        else:
            reasons = ["action_overdue"]
        for reason in reasons:
            self.connection.execute(
                "UPDATE escalations SET state='resolved',resolved_at=?,resolution_note=? "
                "WHERE notice_id=? AND asset_id=? AND reason_code=? AND state='open'",
                (now_text, f"回执 {action} 已登记（第 {round_no} 轮）", notice_id, asset_id, reason),
            )

    def _receipt_response(
        self, receipt: sqlite3.Row, action_row: sqlite3.Row, *, duplicate: bool
    ) -> dict[str, Any]:
        return {
            "receipt_id": int(receipt["receipt_id"]),
            "notice_id": receipt["notice_id"],
            "asset_id": receipt["asset_id"],
            "action": receipt["action"],
            "round_no": int(receipt["round_no"]),
            "recorded_holder_id": receipt["recorded_holder_id"],
            "recorded_at": receipt["recorded_at"],
            "physical_stage": action_row["physical_stage"],
            "notified_round": int(action_row["notified_round"]),
            "acknowledged_round": int(action_row["acknowledged_round"]),
            "duplicate": duplicate,
        }

    # ── 无法联系与逾期升级 ─────────────────────────────────────

    def mark_unreachable(
        self, actor_id: str, notice_id: str, asset_id: str, detail: str
    ) -> dict[str, Any]:
        self._require(actor_id, "escalation.write")
        self._notice(notice_id)
        action = self.connection.execute(
            "SELECT * FROM asset_actions WHERE notice_id=? AND asset_id=? AND in_scope=1",
            (notice_id, asset_id),
        ).fetchone()
        if action is None:
            raise NotFound("资产不在当前召回范围内")
        detail = required_text(detail, "detail", 500)
        return self._open_escalation(notice_id, asset_id, action["current_holder_id"],
                                    int(action["notified_round"]), "unreachable", detail, actor_id)

    def _open_escalation(
        self, notice_id: str, asset_id: str, holder_id: str | None, round_no: int,
        reason_code: str, detail: str, actor_id: str,
    ) -> dict[str, Any]:
        now_text = self._now()
        existing = self.connection.execute(
            "SELECT * FROM escalations WHERE notice_id=? AND asset_id=? AND reason_code=? AND state='open'",
            (notice_id, asset_id, reason_code),
        ).fetchone()
        if existing is not None:
            return {"escalation_id": int(existing["escalation_id"]), "state": "open", "already_open": True}
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO escalations(notice_id,asset_id,holder_id,round_no,reason_code,"
                    "detail,state,opened_at) VALUES(?,?,?,?,?,?, 'open',?)",
                    (notice_id, asset_id, holder_id, round_no, reason_code, detail, now_text),
                )
                escalation_id = int(cursor.lastrowid)
                self._audit("notice", notice_id, "escalation.opened", actor_id, {
                    "escalation_id": escalation_id,
                    "asset_id": asset_id,
                    "holder_id": holder_id,
                    "reason_code": reason_code,
                    "detail": detail,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("该资产已有同类未处理升级") from exc
        return {"escalation_id": escalation_id, "state": "open", "reason_code": reason_code}

    def resolve_escalation(self, actor_id: str, escalation_id: int, note: str) -> dict[str, Any]:
        self._require(actor_id, "escalation.resolve")
        note = required_text(note, "note", 500)
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM escalations WHERE escalation_id=?", (escalation_id,)
            ).fetchone()
            if row is None:
                raise NotFound("升级记录不存在")
            if row["state"] != "open":
                raise InvalidState("升级记录已经关闭")
            now_text = self._now()
            self.connection.execute(
                "UPDATE escalations SET state='resolved',resolved_at=?,resolution_note=? WHERE escalation_id=?",
                (now_text, note, escalation_id),
            )
            self._audit("notice", row["notice_id"], "escalation.resolved", actor_id, {
                "escalation_id": escalation_id,
                "asset_id": row["asset_id"],
                "reason_code": row["reason_code"],
                "note": note,
            })
        return {"escalation_id": escalation_id, "state": "resolved"}

    def scan_overdue(self, actor_id: str, notice_id: str) -> dict[str, Any]:
        """按通知中的时限扫描逾期动作与无法联系持有人，全部进入升级队列。"""

        self._require(actor_id, "escalation.write")
        notice = self._notice(notice_id)
        now = self.clock.now()
        opened: list[dict[str, Any]] = []
        actions = self.connection.execute(
            "SELECT * FROM asset_actions WHERE notice_id=? AND in_scope=1", (notice_id,)
        ).fetchall()
        for action in actions:
            def escalate(reason: str, detail: str, holder: str | None = None, round_no: int = 0) -> None:
                result = self._open_escalation(
                    notice_id, action["asset_id"],
                    action["current_holder_id"] if holder is None else holder,
                    int(action["notified_round"]) if round_no == 0 else round_no,
                    reason, detail, actor_id,
                )
                if not result.get("already_open"):
                    opened.append(result)

            if action["current_holder_id"] is None:
                escalate("unreachable", "没有有效所有权版本，无法确定当前持有人")
                continue
            if int(action["notified_round"]) == 0:
                continue  # 尚未通知由通知覆盖率指标暴露，不产生时限逾期
            current_round = int(action["notified_round"])
            notify_at = self._latest_receipt_at(notice_id, action["asset_id"], "notify", current_round)
            ack_at = self._latest_receipt_at(notice_id, action["asset_id"], "acknowledge", current_round)
            if ack_at is None:
                if notify_at is not None and self._past_due(notify_at, notice["ack_within_hours"], now):
                    escalate("no_acknowledgement",
                             f"签收超过 {notice['ack_within_hours']} 小时时限")
                continue
            stage = action["physical_stage"]
            if stage == "pending":
                if self._past_due(ack_at, notice["quarantine_within_hours"], now):
                    escalate("action_overdue", f"隔离超过 {notice['quarantine_within_hours']} 小时时限")
            elif stage == "quarantined":
                done_at = self._latest_receipt_at(notice_id, action["asset_id"], "quarantine")
                if done_at and self._past_due(done_at, notice["inspect_within_hours"], now):
                    escalate("action_overdue", f"现场检查超过 {notice['inspect_within_hours']} 小时时限")
            elif stage == "inspected":
                done_at = self._latest_receipt_at(notice_id, action["asset_id"], "inspect")
                if done_at and self._past_due(done_at, notice["return_within_hours"], now):
                    escalate("action_overdue", f"返厂超过 {notice['return_within_hours']} 小时时限")
        open_rows = self.connection.execute(
            "SELECT escalation_id,asset_id,holder_id,round_no,reason_code,detail,opened_at "
            "FROM escalations WHERE notice_id=? AND state='open' ORDER BY opened_at,escalation_id",
            (notice_id,),
        ).fetchall()
        return {
            "notice_id": notice_id,
            "scanned_assets": len(actions),
            "opened": opened,
            "open_escalations": [dict(row) for row in open_rows],
        }

    @staticmethod
    def _past_due(done_at_text: str, within_hours: int, now) -> bool:
        return parse_utc(done_at_text) + timedelta(hours=within_hours) < now

    def _latest_receipt_at(
        self, notice_id: str, asset_id: str, action: str, max_round: int | None = None
    ) -> str | None:
        sql = (
            "SELECT max(recorded_at) AS at FROM action_receipts "
            "WHERE notice_id=? AND asset_id=? AND action=?"
        )
        params: list[Any] = [notice_id, asset_id, action]
        if max_round is not None:
            sql += " AND round_no=?"
            params.append(max_round)
        row = self.connection.execute(sql, params).fetchone()
        return row["at"]

    # ── 查询、仪表盘与审计复算 ─────────────────────────────────

    def scope_version(
        self, actor_id: str, notice_id: str, version_no: int | str, *, audit_permission: str = "report.read"
    ) -> dict[str, Any]:
        self._require(actor_id, audit_permission)
        row = self.connection.execute(
            "SELECT * FROM scope_versions WHERE notice_id=? AND version_no=?",
            (notice_id, int(version_no)),
        ).fetchone()
        if row is None:
            raise NotFound("范围版本不存在")
        members = [
            {
                "asset_id": item["asset_id"],
                "asset_kind": item["asset_kind"],
                "holder_id": item["holder_id"],
                "ownership_kind": item["ownership_kind"],
                "capacity_kwh": item["capacity_kwh"],
                "top_level": bool(item["top_level"]),
            }
            for item in self.connection.execute(
                "SELECT asset_id,asset_kind,holder_id,ownership_kind,capacity_kwh,"
                "top_level FROM scope_version_members WHERE scope_version_id=? ORDER BY asset_id",
                (row["scope_version_id"],),
            ).fetchall()
        ]
        return {
            "notice_id": notice_id,
            "version_no": row["version_no"],
            "state": row["state"],
            "direction": row["direction"],
            "spread_direction": row["spread_direction"],
            "reason": row["reason"],
            "as_of": row["as_of"],
            "seeds": json.loads(row["seeds_json"]),
            "base_member_ids": json.loads(row["base_member_ids_json"]),
            "lineage_revision_ids": json.loads(row["lineage_revision_ids_json"]),
            "input_sha256": row["input_sha256"],
            "output_sha256": row["output_sha256"],
            "affected_assets": row["affected_assets"],
            "affected_top_level_capacity_kwh": row["affected_top_level_capacity_kwh"],
            "holder_counts": json.loads(row["holder_counts_json"]),
            "change_summary": json.loads(row["change_summary_json"]),
            "shrink_approved_by": row["shrink_approved_by"],
            "shrink_approved_at": row["shrink_approved_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "members": members,
        }

    def scope_history(self, actor_id: str, notice_id: str) -> dict[str, Any]:
        """每次范围变化的原因、方向、差异与审批痕迹，版本只增不减。"""

        self._require(actor_id, "report.read")
        self._notice(notice_id)
        rows = self.connection.execute(
            "SELECT version_no,direction,spread_direction,reason,state,change_summary_json,affected_assets,"
            "affected_top_level_capacity_kwh,input_sha256,output_sha256,created_by,created_at,"
            "shrink_approved_by,shrink_approved_at,as_of "
            "FROM scope_versions WHERE notice_id=? ORDER BY version_no",
            (notice_id,),
        ).fetchall()
        return {
            "notice_id": notice_id,
            "versions": [
                {
                    "version_no": row["version_no"],
                    "direction": row["direction"],
                    "spread_direction": row["spread_direction"],
                    "state": row["state"],
                    "reason": row["reason"],
                    "as_of": row["as_of"],
                    "change_summary": json.loads(row["change_summary_json"]),
                    "affected_assets": row["affected_assets"],
                    "affected_top_level_capacity_kwh": row["affected_top_level_capacity_kwh"],
                    "input_sha256": row["input_sha256"],
                    "output_sha256": row["output_sha256"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "shrink_approved_by": row["shrink_approved_by"],
                    "shrink_approved_at": row["shrink_approved_at"],
                }
                for row in rows
            ],
        }

    def asset_tracking(self, actor_id: str, notice_id: str, asset_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        action = self.connection.execute(
            "SELECT * FROM asset_actions WHERE notice_id=? AND asset_id=?",
            (notice_id, asset_id),
        ).fetchone()
        if action is None:
            raise NotFound("该召回下没有此资产的编排记录")
        receipts = [
            dict(row) for row in self.connection.execute(
                "SELECT receipt_id,action,round_no,recorded_holder_id,recorded_by,recorded_at,note "
                "FROM action_receipts WHERE notice_id=? AND asset_id=? ORDER BY receipt_id",
                (notice_id, asset_id),
            ).fetchall()
        ]
        return {
            "notice_id": notice_id,
            "asset_id": asset_id,
            "current_holder_id": action["current_holder_id"],
            "physical_stage": action["physical_stage"],
            "notified_round": int(action["notified_round"]),
            "last_notified_holder_id": action["last_notified_holder_id"],
            "acknowledged_round": int(action["acknowledged_round"]),
            "in_scope": bool(action["in_scope"]),
            "first_notified_at": action["first_notified_at"],
            "latest_at": action["latest_at"],
            "receipts": receipts,
        }

    def dashboard(self, actor_id: str, notice_id: str) -> dict[str, Any]:
        """召回负责人视图：范围变化原因、当前责任方、逾期动作与仍在运行的受影响容量。"""

        self._require(actor_id, "report.read")
        notice = self._notice(notice_id)
        active = self._active_version(notice_id)
        if active is None:
            raise InvalidState("召回范围尚未计算")
        actions = self.connection.execute(
            "SELECT aa.*, a.capacity_kwh, a.top_level, a.asset_kind "
            "FROM asset_actions aa JOIN assets a ON a.asset_id=aa.asset_id "
            "WHERE aa.notice_id=?",
            (notice_id,),
        ).fetchall()
        in_scope = [row for row in actions if row["in_scope"]]
        running_capacity = Decimal("0")
        notified = 0
        acknowledged = 0
        stage_counts: dict[str, int] = {}
        holder_counts: dict[str, int] = {}
        for row in in_scope:
            stage_counts[row["physical_stage"]] = stage_counts.get(row["physical_stage"], 0) + 1
            # 已返厂的资产物理上已离开运行现场，不计入仍在运行容量；
            # 隔离与现场检查中的资产仍挂接在运行系统上，继续计入。
            if row["physical_stage"] not in {"returned", "released"} and row["top_level"]:
                running_capacity += Decimal(row["capacity_kwh"])
            if int(row["notified_round"]) > 0:
                notified += 1
            if int(row["acknowledged_round"]) >= int(row["notified_round"]) and int(row["notified_round"]) > 0:
                acknowledged += 1
            holder = row["current_holder_id"] or "(unknown)"
            holder_counts[holder] = holder_counts.get(holder, 0) + 1
        open_escalations = [
            dict(row) for row in self.connection.execute(
                "SELECT escalation_id,asset_id,holder_id,round_no,reason_code,detail,opened_at "
                "FROM escalations WHERE notice_id=? AND state='open' ORDER BY opened_at,escalation_id",
                (notice_id,),
            ).fetchall()
        ]
        notified_but_removed = sum(
            1 for row in actions if not row["in_scope"] and int(row["notified_round"]) > 0
        )
        return {
            "notice_id": notice_id,
            "notice_state": notice["state"],
            "current_version": {
                "version_no": active["version_no"],
                "direction": active["direction"],
                "reason": active["reason"],
                "as_of": active["as_of"],
                "created_by": active["created_by"],
                "created_at": active["created_at"],
                "input_sha256": active["input_sha256"],
                "output_sha256": active["output_sha256"],
            },
            "scope": {
                "assets_in_scope": len(in_scope),
                "affected_top_level_capacity_kwh": active["affected_top_level_capacity_kwh"],
                "running_top_level_capacity_kwh": decimal_text(quantize_kwh(running_capacity)),
                "notified_assets": notified,
                "acknowledged_assets": acknowledged,
                "stage_counts": dict(sorted(stage_counts.items())),
                "current_holders": dict(sorted(holder_counts.items())),
                "removed_but_ever_notified_assets": notified_but_removed,
            },
            "open_escalations": open_escalations,
            "overdue_count": len(open_escalations),
        }

    def recompute_scope(self, actor_id: str, notice_id: str, version_no: int) -> dict[str, Any]:
        """审计入口：用版本固化的快照与选择输入重新计算，逐字段比对。"""

        self._require(actor_id, "audit.read")
        row = self.connection.execute(
            "SELECT * FROM scope_versions WHERE notice_id=? AND version_no=?",
            (notice_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound("范围版本不存在")
        snapshot = json.loads(row["lineage_snapshot_json"])
        seeds = json.loads(row["seeds_json"])
        base_member_ids = json.loads(row["base_member_ids_json"])
        direction = row["spread_direction"]
        lineage = self._freeze_lineage(snapshot)
        request = ScopeRequest(
            notice_id=notice_id,
            seeds=seeds,
            direction=direction,
            as_of=row["as_of"],
            reason=row["reason"],
            lineage_revision_ids=json.loads(row["lineage_revision_ids_json"]),
            base_member_ids=base_member_ids,
        )
        recomputed = compute_scope(lineage, request)
        stored_members = [
            {
                "asset_id": item["asset_id"],
                "asset_kind": item["asset_kind"],
                "holder_id": item["holder_id"],
                "ownership_kind": item["ownership_kind"],
                "capacity_kwh": item["capacity_kwh"],
                "top_level": bool(item["top_level"]),
            }
            for item in self.connection.execute(
                "SELECT asset_id,asset_kind,holder_id,ownership_kind,capacity_kwh,top_level "
                "FROM scope_version_members WHERE scope_version_id=? ORDER BY asset_id",
                (row["scope_version_id"],),
            ).fetchall()
        ]
        matches = (
            recomputed["output_sha256"] == row["output_sha256"]
            and recomputed["input_sha256"] == row["input_sha256"]
            and recomputed["members"] == stored_members
            and recomputed["affected_assets"] == row["affected_assets"]
            and recomputed["affected_top_level_capacity_kwh"] == row["affected_top_level_capacity_kwh"]
        )
        return {
            "notice_id": notice_id,
            "version_no": version_no,
            "matches": matches,
            "stored": {
                "input_sha256": row["input_sha256"],
                "output_sha256": row["output_sha256"],
                "affected_assets": row["affected_assets"],
                "affected_top_level_capacity_kwh": row["affected_top_level_capacity_kwh"],
                "members": stored_members,
            },
            "recomputed": recomputed,
        }

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
