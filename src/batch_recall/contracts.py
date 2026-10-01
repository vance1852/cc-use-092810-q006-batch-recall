"""批次召回编排的严格输入契约。

谱系边与所有权版本只增不改（append-only）：范围版本一旦基于某个
谱系修订与所有权版本序列冻结，之后任何复算都会得到相同结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 召回措施按资产独立推进的六个阶段，顺序即依赖顺序。
ACTION_STAGES: tuple[str, ...] = (
    "notify",      # 通知
    "acknowledge",  # 签收
    "quarantine",   # 隔离
    "inspect",      # 现场检查
    "return_to_oem",  # 返厂
    "release",      # 解除措施
)
STAGE_INDEX: dict[str, int] = {stage: index for index, stage in enumerate(ACTION_STAGES)}

OWNERSHIP_MODES = frozenset({"held", "in_transfer"})
EDGE_KINDS = frozenset({"assembly", "installed", "split", "resale", "lease", "repair", "dismantle"})
CHANGE_KINDS = frozenset({"initial", "expand", "reduce"})
CHANGE_STATUS = frozenset({"effective", "pending_approval", "superseded"})
ESCALATION_REASONS = frozenset({"unreachable", "overdue", "rejected"})


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def sha256_text(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制 SHA-256")
    return result.lower()


def timestamp(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return parse_utc(text, field).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SupplierNotice:
    """电芯供应商发布的批次风险通知。"""

    notice_id: str
    supplier_id: str
    component_lot_id: str
    title: str
    risk_summary: str
    issued_at: str
    evidence_sha256: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplierNotice":
        return cls(
            notice_id=identifier(raw.get("notice_id"), "notice_id"),
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            component_lot_id=identifier(raw.get("component_lot_id"), "component_lot_id"),
            title=required_text(raw.get("title"), "title"),
            risk_summary=required_text(raw.get("risk_summary"), "risk_summary", 2000),
            issued_at=timestamp(raw.get("issued_at"), "issued_at"),
            evidence_sha256=sha256_text(raw.get("evidence_sha256"), "evidence_sha256"),
        )


@dataclass(frozen=True, slots=True)
class GenealogyEdge:
    """一条冻结的组件谱系证据：parent 在 observed_at 时刻包含 child。

    方向为上下游包含关系（电芯→模组→电池包→场站），风险沿边双向扩散。
    """

    edge_id: str
    parent_id: str
    child_id: str
    edge_kind: str
    observed_at: str
    evidence_sha256: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GenealogyEdge":
        parent_id = identifier(raw.get("parent_id"), "parent_id")
        child_id = identifier(raw.get("child_id"), "child_id")
        if parent_id == child_id:
            raise ValidationFailed("谱系边的上下游资产不能相同")
        edge_kind = required_text(raw.get("edge_kind"), "edge_kind", 24)
        if edge_kind not in EDGE_KINDS:
            raise ValidationFailed("edge_kind 不是受支持的谱系关系")
        return cls(
            edge_id=identifier(raw.get("edge_id"), "edge_id"),
            parent_id=parent_id,
            child_id=child_id,
            edge_kind=edge_kind,
            observed_at=timestamp(raw.get("observed_at"), "observed_at"),
            evidence_sha256=sha256_text(raw.get("evidence_sha256"), "evidence_sha256"),
            note=required_text(raw.get("note", ""), "note", 512) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class OwnershipVersion:
    """资产所有权的一个有效版本（只增不改）。

    mode=held 时 asset_id 必须是可单独持有的资产；mode=in_transfer 表示
    所有权转移尚未完成，资产仍保留召回约束，义务方为当前登记持有人。
    """

    asset_id: str
    version: int
    holder_id: str
    mode: str
    effective_at: str
    contact_channel: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OwnershipVersion":
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationFailed("version 必须是正整数")
        mode = required_text(raw.get("mode"), "mode", 16)
        if mode not in OWNERSHIP_MODES:
            raise ValidationFailed("mode 必须是 held 或 in_transfer")
        return cls(
            asset_id=identifier(raw.get("asset_id"), "asset_id"),
            version=version,
            holder_id=identifier(raw.get("holder_id"), "holder_id"),
            mode=mode,
            effective_at=timestamp(raw.get("effective_at"), "effective_at"),
            contact_channel=required_text(raw.get("contact_channel", ""), "contact_channel", 128)
            if raw.get("contact_channel")
            else "",
        )
