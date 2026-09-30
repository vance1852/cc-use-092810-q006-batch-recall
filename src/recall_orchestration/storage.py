"""批次召回编排的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS recall_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('recall_lead','field_agent','approver','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    notice_id TEXT PRIMARY KEY,
    supplier_batch_id TEXT NOT NULL,
    title TEXT NOT NULL,
    risk_description TEXT NOT NULL,
    supplier_ref TEXT NOT NULL,
    ack_within_hours INTEGER NOT NULL DEFAULT 72 CHECK(ack_within_hours > 0),
    quarantine_within_hours INTEGER NOT NULL DEFAULT 168 CHECK(quarantine_within_hours > 0),
    inspect_within_hours INTEGER NOT NULL DEFAULT 336 CHECK(inspect_within_hours > 0),
    return_within_hours INTEGER NOT NULL DEFAULT 720 CHECK(return_within_hours > 0),
    issued_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
    created_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL
);

-- 冻结的谱系证据：只增不改。content_sha256 覆盖修订正文，
-- previous_revision_id/content_sha256 形成证据链，已冻结的修订不可重放。
CREATE TABLE IF NOT EXISTS lineage_revisions (
    revision_id TEXT PRIMARY KEY,
    notice_id TEXT REFERENCES notices(notice_id),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    previous_revision_id TEXT REFERENCES lineage_revisions(revision_id),
    previous_content_sha256 TEXT,
    frozen_at TEXT,
    created_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    asset_kind TEXT NOT NULL,
    capacity_kwh TEXT NOT NULL,
    top_level INTEGER NOT NULL CHECK(top_level IN (0,1)),
    first_seen_revision_id TEXT NOT NULL REFERENCES lineage_revisions(revision_id),
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, first_seen_revision_id)
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    edge_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES lineage_revisions(revision_id),
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('manufactured_from','contained','installed_in','removed_from')),
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_parent ON lineage_edges(parent_id, revision_id);
CREATE INDEX IF NOT EXISTS idx_edges_child ON lineage_edges(child_id, revision_id);

-- 有效所有权版本：同一资产版本号严格递增，转移中的资产靠最新版本确定当前责任方。
CREATE TABLE IF NOT EXISTS ownership_versions (
    asset_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    revision_id TEXT NOT NULL REFERENCES lineage_revisions(revision_id),
    holder_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('sale','resale','lease','return_from_lease','repair','split','initial')),
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(asset_id, version),
    UNIQUE(asset_id, version, revision_id)
);

CREATE INDEX IF NOT EXISTS idx_ownership_asset_time ON ownership_versions(asset_id, effective_from, version);

-- 召回范围版本：只增。缩减方向必须经独立批准；记录扩散原因与证据快照。
CREATE TABLE IF NOT EXISTS scope_versions (
    scope_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id TEXT NOT NULL REFERENCES notices(notice_id),
    version_no INTEGER NOT NULL CHECK(version_no > 0),
    direction TEXT NOT NULL CHECK(direction IN ('initial','downstream','upstream','both','shrink')),
    spread_direction TEXT NOT NULL DEFAULT 'downstream'
        CHECK(spread_direction IN ('downstream','upstream','both')),
    reason TEXT NOT NULL,
    as_of TEXT NOT NULL,
    seeds_json TEXT NOT NULL,
    base_member_ids_json TEXT NOT NULL DEFAULT '[]',
    lineage_snapshot_json TEXT NOT NULL,
    lineage_revision_ids_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
    output_sha256 TEXT NOT NULL CHECK(length(output_sha256)=64),
    affected_assets INTEGER NOT NULL,
    affected_top_level_capacity_kwh TEXT NOT NULL,
    holder_counts_json TEXT NOT NULL,
    change_summary_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','active','superseded','rejected')),
    shrink_approved_by TEXT REFERENCES recall_users(user_id),
    shrink_approved_at TEXT,
    created_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(notice_id, version_no)
);

CREATE TABLE IF NOT EXISTS scope_version_members (
    scope_version_id INTEGER NOT NULL REFERENCES scope_versions(scope_version_id),
    asset_id TEXT NOT NULL,
    asset_kind TEXT NOT NULL,
    holder_id TEXT,
    ownership_kind TEXT,
    capacity_kwh TEXT NOT NULL,
    top_level INTEGER NOT NULL CHECK(top_level IN (0,1)),
    PRIMARY KEY(scope_version_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_members_holder ON scope_version_members(scope_version_id, holder_id);

-- 每个资产在召回下一条编排记录；物理阶段单调，通知轮次随所有权转移递增。
CREATE TABLE IF NOT EXISTS asset_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id TEXT NOT NULL REFERENCES notices(notice_id),
    asset_id TEXT NOT NULL,
    current_holder_id TEXT,
    physical_stage TEXT NOT NULL DEFAULT 'pending'
        CHECK(physical_stage IN ('pending','quarantined','inspected','returned','released')),
    notified_round INTEGER NOT NULL DEFAULT 0,
    last_notified_holder_id TEXT,
    acknowledged_round INTEGER NOT NULL DEFAULT 0,
    first_notified_at TEXT,
    latest_at TEXT NOT NULL,
    in_scope INTEGER NOT NULL DEFAULT 1 CHECK(in_scope IN (0,1)),
    UNIQUE(notice_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_actions_holder ON asset_actions(notice_id, current_holder_id);
CREATE INDEX IF NOT EXISTS idx_actions_stage ON asset_actions(notice_id, physical_stage);

-- 回执只增：同一 (通知, 资产, 措施, 轮次) 的重复回执被唯一约束吸收。
CREATE TABLE IF NOT EXISTS action_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id TEXT NOT NULL REFERENCES notices(notice_id),
    asset_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('notify','acknowledge','quarantine','inspect','return_to_factory','release')),
    round_no INTEGER NOT NULL CHECK(round_no > 0),
    recorded_holder_id TEXT,
    idempotency_key TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL REFERENCES recall_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(notice_id, asset_id, action, round_no),
    UNIQUE(idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_receipts_asset ON action_receipts(notice_id, asset_id, receipt_id);

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id TEXT NOT NULL REFERENCES notices(notice_id),
    asset_id TEXT NOT NULL,
    holder_id TEXT,
    round_no INTEGER NOT NULL,
    reason_code TEXT NOT NULL
        CHECK(reason_code IN ('unreachable','no_acknowledgement','action_overdue','transfer_pending_notify')),
    detail TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_escalation_per_asset_reason
ON escalations(notice_id, asset_id, reason_code)
WHERE state='open';

CREATE INDEX IF NOT EXISTS idx_escalations_open ON escalations(state, opened_at);

CREATE TABLE IF NOT EXISTS recall_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS recall_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recall_audit_entity
ON recall_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用连接；写事务统一用
    # BEGIN IMMEDIATE + busy_timeout 串行化，跨线程共享是安全的。
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
