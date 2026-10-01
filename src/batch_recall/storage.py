"""批次召回编排的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('coordinator','approver','field','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 供应商批次风险通知
CREATE TABLE IF NOT EXISTS supplier_notices (
    notice_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    component_lot_id TEXT NOT NULL,
    title TEXT NOT NULL,
    risk_summary TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL
);

-- 冻结的组件谱系证据（只增不改）
CREATE TABLE IF NOT EXISTS genealogy_edges (
    edge_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    note TEXT NOT NULL DEFAULT '',
    genealogy_revision INTEGER NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL,
    CHECK (parent_id <> child_id)
);

CREATE INDEX IF NOT EXISTS idx_genealogy_nodes
ON genealogy_edges(parent_id, child_id);

-- 谱系修订水位：每条边归入一个只增修订
CREATE TABLE IF NOT EXISTS genealogy_revisions (
    revision INTEGER PRIMARY KEY CHECK (revision > 0),
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES recall_users(user_id),
    edge_count INTEGER NOT NULL
);

-- 资产主数据：容量用于计算仍在运行的受影响容量
CREATE TABLE IF NOT EXISTS recall_assets (
    asset_id TEXT PRIMARY KEY,
    asset_kind TEXT NOT NULL,
    capacity_kwh TEXT NOT NULL DEFAULT '0',
    running INTEGER NOT NULL DEFAULT 1 CHECK (running IN (0,1)),
    created_at TEXT NOT NULL
);

-- 有效所有权版本（只增不改）
CREATE TABLE IF NOT EXISTS ownership_versions (
    asset_id TEXT NOT NULL REFERENCES recall_assets(asset_id),
    version INTEGER NOT NULL CHECK (version > 0),
    holder_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('held','in_transfer')),
    effective_at TEXT NOT NULL,
    contact_channel TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, version)
);

CREATE INDEX IF NOT EXISTS idx_ownership_holder
ON ownership_versions(holder_id, asset_id);

-- 召回主单
CREATE TABLE IF NOT EXISTS recalls (
    recall_id TEXT PRIMARY KEY,
    notice_id TEXT NOT NULL UNIQUE REFERENCES supplier_notices(notice_id),
    component_lot_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','closed')),
    sla_hours INTEGER NOT NULL CHECK (sla_hours > 0),
    current_scope_version INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL
);

-- 范围版本：任一版本都可由谱系修订+所有权版本+父版本独立复算
CREATE TABLE IF NOT EXISTS scope_versions (
    scope_version INTEGER NOT NULL CHECK (scope_version > 0),
    recall_id TEXT NOT NULL REFERENCES recalls(recall_id),
    change_kind TEXT NOT NULL CHECK (change_kind IN ('initial','expand','reduce')),
    status TEXT NOT NULL CHECK (status IN ('effective','pending_approval','superseded')),
    reason TEXT NOT NULL,
    genealogy_revision INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL CHECK (length(basis_sha256) = 64),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    parent_scope_version INTEGER,
    created_by TEXT NOT NULL REFERENCES recall_users(user_id),
    created_at TEXT NOT NULL,
    decided_by TEXT REFERENCES recall_users(user_id),
    decided_at TEXT,
    decision_note TEXT,
    PRIMARY KEY (recall_id, scope_version)
);

CREATE INDEX IF NOT EXISTS idx_scope_status
ON scope_versions(recall_id, status);

-- 范围版本明细：资产在某版本中的成员关系与证据来源
CREATE TABLE IF NOT EXISTS scope_entries (
    recall_id TEXT NOT NULL,
    scope_version INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    include_state TEXT NOT NULL CHECK (include_state IN ('included','excluded')),
    added_in_version INTEGER NOT NULL,
    match_path_json TEXT NOT NULL,
    first_noticed_at TEXT,
    PRIMARY KEY (recall_id, scope_version, asset_id),
    FOREIGN KEY (recall_id, scope_version) REFERENCES scope_versions(recall_id, scope_version)
);

CREATE INDEX IF NOT EXISTS idx_scope_entries_asset
ON scope_entries(recall_id, asset_id, scope_version);

-- 每资产召回措施（六个阶段独立推进）
CREATE TABLE IF NOT EXISTS asset_actions (
    recall_id TEXT NOT NULL REFERENCES recalls(recall_id),
    asset_id TEXT NOT NULL,
    scope_version INTEGER NOT NULL,
    current_stage TEXT NOT NULL DEFAULT 'notify'
        CHECK (current_stage IN ('notify','acknowledge','quarantine','inspect','return_to_oem','release','closed')),
    in_scope INTEGER NOT NULL DEFAULT 1 CHECK (in_scope IN (0,1)),
    current_due_at TEXT,
    blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0,1)),
    unreachable_flag INTEGER NOT NULL DEFAULT 0 CHECK (unreachable_flag IN (0,1)),
    first_notice_version INTEGER,
    latest_holder_id TEXT,
    latest_owner_version INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (recall_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_actions_holder
ON asset_actions(recall_id, latest_holder_id);

-- 措施阶段回执：append-only，重复/乱序回执不倒退已完成状态
CREATE TABLE IF NOT EXISTS action_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    stage TEXT NOT NULL
        CHECK (stage IN ('notify','acknowledge','quarantine','inspect','return_to_oem','release')),
    state TEXT NOT NULL CHECK (state IN ('done','rejected','duplicate','out_of_order')),
    idempotency_key TEXT,
    holder_id TEXT,
    owner_version INTEGER,
    evidence_sha256 TEXT,
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL REFERENCES recall_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE (recall_id, asset_id, stage, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_receipts_asset
ON action_receipts(recall_id, asset_id, receipt_id);

-- 各阶段完成时间（每资产每阶段最多一个 done 回执）
CREATE TABLE IF NOT EXISTS action_stage_completions (
    recall_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    receipt_id INTEGER NOT NULL REFERENCES action_receipts(receipt_id),
    due_at TEXT,
    completed_at TEXT NOT NULL,
    holder_id TEXT,
    owner_version INTEGER,
    PRIMARY KEY (recall_id, asset_id, stage)
);

-- 无法联系/拒收/逾期持有人的升级队列
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('unreachable','overdue','rejected')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    note TEXT NOT NULL DEFAULT '',
    opened_by TEXT NOT NULL REFERENCES recall_users(user_id),
    opened_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES recall_users(user_id),
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_escalations_status
ON escalations(recall_id, status, opened_at);

-- 哈希链审计事件
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

REQUIRED_TABLES = frozenset({
    "schema_meta", "recall_users", "supplier_notices", "genealogy_edges", "genealogy_revisions",
    "recall_assets", "ownership_versions", "recalls", "scope_versions", "scope_entries",
    "asset_actions", "action_receipts", "action_stage_completions", "escalations",
    "recall_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用外键与显式事务设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化召回表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
