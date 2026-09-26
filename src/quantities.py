"""数量值对象与箱盒换算。

现场清点常以“几箱零几盒”记录，票据与台账又多按最小销售单位记录。
所有数量在进入比对前统一换算到商品资料声明的最小单位，换算不上来的
不做猜测，直接留给人工处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class QuantityError(ValueError):
    """数量无法按商品资料换算。"""


@dataclass(frozen=True)
class Qty:
    value: int
    unit: str

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.value}{self.unit}"


def parse_qty(spec: Any, product_unit: str, per_box: int | None = None) -> Qty:
    """把 {"value": 10, "unit": "盒"} 或 {"boxes": 2, "per_box": 20} 换算为最小单位。"""
    if not isinstance(spec, dict):
        raise QuantityError(f"数量格式不支持: {spec!r}")

    unit = spec.get("unit", product_unit)
    if unit != product_unit:
        raise QuantityError(f"计量单位 {unit} 与商品登记单位 {product_unit} 不一致")

    if "value" in spec:
        value = spec["value"]
    elif "boxes" in spec:
        box_per = spec.get("per_box", per_box)
        if not box_per:
            raise QuantityError("按箱登记但缺少每箱换算量")
        value = spec["boxes"] * box_per
        loose = spec.get("loose", 0)
        value += loose
    else:
        raise QuantityError(f"数量缺少 value 或 boxes: {spec!r}")

    if not isinstance(value, int) or value < 0:
        raise QuantityError(f"数量必须是非负整数: {spec!r}")
    return Qty(value, unit)


def add(a: Qty | None, b: Qty | None) -> Qty | None:
    if a is None:
        return b
    if b is None:
        return a
    if a.unit != b.unit:
        raise QuantityError(f"不能跨单位相加: {a.unit} / {b.unit}")
    return Qty(a.value + b.value, a.unit)
