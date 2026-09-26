"""四向核验领域模型。

四类原始依据：
  票 —— 发票与随货同行单（更正只追加新版本）
  账 —— 进销存片段
  货 —— 现场箱盒清点
  款 —— 银行付款摘要

补充单据：退货单、折让单，用于解释数量差与金额差，本身不构成违规。
金额一律以“分”为单位的整数，避免浮点误差进入比对结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .quantities import Qty


class EvidenceKind(str, Enum):
    INVOICE = "invoice"            # 发票
    DELIVERY_NOTE = "delivery_note"  # 随货同行单
    LEDGER = "ledger"              # 进销存片段
    STOCK_COUNT = "stock_count"    # 现场清点
    PAYMENT = "payment"            # 银行付款摘要
    RETURN_NOTE = "return_note"    # 退货单
    ALLOWANCE = "allowance"        # 折让单
    EXPLANATION = "explanation"    # 供应商/药店提交的说明


class PartyRole(str, Enum):
    SUPPLIER = "supplier"
    PHARMACY = "pharmacy"


@dataclass(frozen=True)
class Party:
    party_id: str
    name: str
    role: PartyRole
    license_no: str = ""


@dataclass(frozen=True)
class Relation:
    """采购关系：匹配候选只沿关系提出，不跨关系撮合。"""

    relation_id: str
    supplier_id: str
    pharmacy_id: str
    valid_from: date
    valid_to: date | None = None

    def covers(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to


@dataclass(frozen=True)
class InvoiceLine:
    product_id: str
    qty: Qty
    unit_price_fen: int
    batch_no: str = ""

    @property
    def amount_fen(self) -> int:
        return self.qty.value * self.unit_price_fen


@dataclass(frozen=True)
class Invoice:
    """一张发票的一个版本。payment_due 为赊销约定付款日，空表示现结。"""

    invoice_id: str
    version: int
    supplier_id: str
    pharmacy_id: str
    issue_date: date
    lines: tuple[InvoiceLine, ...]
    template_id: str = ""          # 票据模板标识，用于跨门店串用发现
    payment_due: date | None = None
    supersedes: str | None = None  # 被本版本更正的上一版本号

    @property
    def total_fen(self) -> int:
        return sum(line.amount_fen for line in self.lines)

    @property
    def version_key(self) -> str:
        return f"{self.invoice_id}@v{self.version}"


@dataclass(frozen=True)
class DeliveryNote:
    """随货同行单。分批送货时一张发票对应多张同行单。"""

    note_no: str
    invoice_id: str
    supplier_id: str
    pharmacy_id: str
    ship_date: date
    lines: tuple[InvoiceLine, ...]  # 复用行结构，unit_price 可填 0


@dataclass(frozen=True)
class LedgerEntry:
    """进销存片段中的一行。"""

    entry_id: str
    pharmacy_id: str
    product_id: str
    change: int          # 正为入库，负为出库/销售
    unit: str
    entry_date: date
    source_doc: str = ""  # 关联的随货同行单号或发票号，可为空


@dataclass(frozen=True)
class StockCount:
    """现场清点：某药店某商品在某日的实盘数。"""

    count_id: str
    pharmacy_id: str
    product_id: str
    qty: Qty
    count_date: date


@dataclass(frozen=True)
class Payment:
    """银行付款摘要。一笔付款可合并支付多张发票（invoice_ids 多个）。"""

    payment_id: str
    payer_id: str
    payee_id: str
    amount_fen: int
    pay_date: date
    invoice_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReturnNote:
    return_id: str
    invoice_id: str
    product_id: str
    qty: Qty
    return_date: date


@dataclass(frozen=True)
class Allowance:
    """折让：只减金额，不减数量。"""

    allowance_id: str
    invoice_id: str
    amount_fen: int
    allowance_date: date


@dataclass(frozen=True)
class Explanation:
    """供应商或药店对某条差异提交的说明，按提交方权限隔离。"""

    explanation_id: str
    difference_id: str
    author_party_id: str
    text: str
    submitted_at: date
