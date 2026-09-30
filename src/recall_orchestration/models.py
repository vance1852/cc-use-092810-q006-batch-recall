"""批次召回编排的输入契约与校验辅助。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
DIRECTIONS = {"downstream", "upstream", "both"}
KINDS = {"manufactured_from", "contained", "installed_in", "removed_from"}
OWNERSHIP_KINDS = {"sale", "resale", "lease", "return_from_lease", "repair", "split"}
STAGES = ("notified", "acknowledged", "quarantined", "inspected", "returned", "released")
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}


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
    return result


def timestamp(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return utc_text(parse_utc(text, field))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def stage_label(stage: str) -> str:
    labels = {
        "notified": "通知",
        "acknowledged": "签收",
        "quarantined": "隔离",
        "inspected": "现场检查",
        "returned": "返厂",
        "released": "解除",
    }
    return labels[stage]
