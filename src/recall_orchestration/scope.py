"""从冻结谱系快照与有效所有权版本确定性计算召回范围。

本模块不访问数据库：给定同一份快照与选择规则，任何审计者都能复算出
完全相同的成员集合与摘要，包括相同的输入/输出指纹。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .lineage import FrozenLineage, effective_owner, spread_from_seeds


SELECTOR_RULES_VERSION = "recall-scope-rules-1"


def quantize_kwh(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScopeRequest:
    notice_id: str
    seeds: Sequence[str]
    direction: str
    as_of: str
    reason: str
    lineage_revision_ids: Sequence[str]
    base_member_ids: Sequence[str] = ()

    def input_record(self) -> dict[str, object]:
        return {
            "notice_id": self.notice_id,
            "seeds": sorted(self.seeds),
            "base_member_ids": sorted(self.base_member_ids),
            "direction": self.direction,
            "as_of": self.as_of,
            "reason": self.reason,
            "lineage_revision_ids": sorted(self.lineage_revision_ids),
            "selector_rules_version": SELECTOR_RULES_VERSION,
        }


def compute_scope(lineage: FrozenLineage, request: ScopeRequest) -> dict[str, object]:
    """计算范围成员及其当前责任方，并给出可复算的输入/输出指纹。

    扩散类版本把当前活动成员并入遍历根（根必中），因此范围只累积不回退；
    初始与缩减版本的 base_member_ids 为空，完全由批次种子决定。
    """

    roots = tuple(dict.fromkeys((*request.seeds, *request.base_member_ids)))
    affected = spread_from_seeds(lineage, roots, request.direction)
    members: list[dict[str, object]] = []
    total_capacity = Decimal("0")
    holders: dict[str, int] = {}
    for asset_id in sorted(affected):
        node = lineage.nodes[asset_id]
        owner = effective_owner(lineage, asset_id, request.as_of)
        holder_id = None if owner is None else owner.holder_id
        members.append({
            "asset_id": asset_id,
            "asset_kind": node.kind,
            "holder_id": holder_id,
            "ownership_kind": None if owner is None else owner.kind,
            "capacity_kwh": decimal_text(quantize_kwh(node.capacity_kwh)),
            "top_level": node.top_level,
        })
        if node.top_level:
            total_capacity += node.capacity_kwh
        if holder_id is not None:
            holders[holder_id] = holders.get(holder_id, 0) + 1
    input_sha256 = digest(request.input_record())
    output_record = {"members": members}
    return {
        "input_sha256": input_sha256,
        "output_sha256": digest(output_record),
        "affected_assets": len(members),
        "affected_top_level_capacity_kwh": decimal_text(quantize_kwh(total_capacity)),
        "holder_counts": dict(sorted(holders.items())),
        "members": members,
    }
