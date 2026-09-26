"""查看权限。

检查方（检查员、负责人）可以看全部材料与双方说明；
供应商只能看本方提交的说明，看不到药店的说明；药店反之。
自动匹配只产出候选与差异，权限判断同样适用于说明的回显。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import Explanation


class Role(str, Enum):
    INSPECTOR = "inspector"  # 检查员
    LEAD = "lead"            # 负责人
    SUPPLIER = "supplier"    # 供应商账号
    PHARMACY = "pharmacy"    # 药店账号


_INSPECTOR_ROLES = {Role.INSPECTOR, Role.LEAD}


@dataclass(frozen=True)
class Viewer:
    role: Role
    party_id: str | None = None  # 供应商/药店账号绑定的主体

    def can_see_explanation(self, explanation: Explanation) -> bool:
        if self.role in _INSPECTOR_ROLES:
            return True
        if self.role in (Role.SUPPLIER, Role.PHARMACY):
            return self.party_id == explanation.author_party_id
        return False  # pragma: no cover - 枚举穷尽


def filter_explanations(
    explanations: list[Explanation], viewer: Viewer
) -> list[Explanation]:
    return [e for e in explanations if viewer.can_see_explanation(e)]


def can_submit_explanation(viewer: Viewer, author_party_id: str) -> bool:
    """供应商/药店只能以自己的主体名义提交说明，检查方不代为提交。"""
    if viewer.role not in (Role.SUPPLIER, Role.PHARMACY):
        return False
    return viewer.party_id == author_party_id
