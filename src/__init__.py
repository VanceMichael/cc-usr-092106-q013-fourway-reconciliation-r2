"""票账货款四向核验领域内核。

- contracts: 材料 JSON 契约校验
- registry:  材料提交的只追加版本登记
- matcher:   按采购关系的四向匹配候选与四类差异（不作违规结论）
- casework:  人工决定、整改追溯、说明权限隔离、票据模板串并
"""

from .casework import Actor, Casework
from .matcher import run_reconciliation
from .registry import SubmissionRegistry

__all__ = ["Actor", "Casework", "SubmissionRegistry", "run_reconciliation"]
