"""材料登记：只追加，不覆盖，不删除。

更正一张发票意味着追加一个 version 更高的 Invoice，旧版本仍然可查，
差异溯源时必须能回到“当时看到的是哪一版”。
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import (
    Allowance,
    DeliveryNote,
    EvidenceKind,
    Explanation,
    Invoice,
    LedgerEntry,
    Payment,
    ReturnNote,
    StockCount,
)


class VersionConflict(ValueError):
    """试图覆盖或回退已有版本。"""


class EvidenceStore:
    """按种类分桶的只追加登记处。"""

    def __init__(self) -> None:
        self._invoices: dict[str, list[Invoice]] = {}
        self._delivery_notes: list[DeliveryNote] = []
        self._ledger: list[LedgerEntry] = []
        self._counts: list[StockCount] = []
        self._payments: list[Payment] = []
        self._returns: list[ReturnNote] = []
        self._allowances: list[Allowance] = []
        self._explanations: list[Explanation] = []

    # ---- 登记 ----

    def add_invoice(self, invoice: Invoice) -> None:
        versions = self._invoices.setdefault(invoice.invoice_id, [])
        if any(v.version == invoice.version for v in versions):
            raise VersionConflict(
                f"发票 {invoice.invoice_id} 第 {invoice.version} 版已存在，更正请追加新版本"
            )
        if versions and invoice.version <= max(v.version for v in versions):
            raise VersionConflict(f"发票 {invoice.invoice_id} 版本号必须递增")
        versions.append(invoice)

    def add_delivery_note(self, note: DeliveryNote) -> None:
        self._delivery_notes.append(note)

    def add_ledger(self, entry: LedgerEntry) -> None:
        self._ledger.append(entry)

    def add_stock_count(self, count: StockCount) -> None:
        self._counts.append(count)

    def add_payment(self, payment: Payment) -> None:
        self._payments.append(payment)

    def add_return(self, note: ReturnNote) -> None:
        self._returns.append(note)

    def add_allowance(self, allowance: Allowance) -> None:
        self._allowances.append(allowance)

    def add_explanation(self, explanation: Explanation) -> None:
        self._explanations.append(explanation)

    # ---- 查询 ----

    def invoice_versions(self, invoice_id: str) -> list[Invoice]:
        return sorted(self._invoices.get(invoice_id, []), key=lambda v: v.version)

    def latest_invoice(self, invoice_id: str) -> Invoice | None:
        versions = self.invoice_versions(invoice_id)
        return versions[-1] if versions else None

    def invoices(self) -> list[Invoice]:
        """每张发票的最新版本，供匹配使用。"""
        return [vs[-1] for vs in self._invoices.values() if vs]

    def all_invoice_versions(self) -> list[Invoice]:
        return [v for vs in self._invoices.values() for v in vs]

    def delivery_notes(self, invoice_id: str | None = None) -> list[DeliveryNote]:
        if invoice_id is None:
            return list(self._delivery_notes)
        return [n for n in self._delivery_notes if n.invoice_id == invoice_id]

    def ledger(self) -> list[LedgerEntry]:
        return list(self._ledger)

    def stock_counts(self) -> list[StockCount]:
        return list(self._counts)

    def payments(self) -> list[Payment]:
        return list(self._payments)

    def returns(self) -> list[ReturnNote]:
        return list(self._returns)

    def allowances(self) -> list[Allowance]:
        return list(self._allowances)

    def explanations(self) -> list[Explanation]:
        return list(self._explanations)

    def load(self, bundle: "EvidenceBundle") -> None:
        """从解析好的资料包批量登记。"""
        for invoice in bundle.invoices:
            self.add_invoice(invoice)
        for note in bundle.delivery_notes:
            self.add_delivery_note(note)
        for entry in bundle.ledger:
            self.add_ledger(entry)
        for count in bundle.stock_counts:
            self.add_stock_count(count)
        for payment in bundle.payments:
            self.add_payment(payment)
        for note in bundle.returns:
            self.add_return(note)
        for allowance in bundle.allowances:
            self.add_allowance(allowance)


def kinds_of(store: EvidenceStore) -> Iterable[EvidenceKind]:
    """登记处当前实际收到的依据种类，用于检查四向是否齐全。"""
    if store.all_invoice_versions():
        yield EvidenceKind.INVOICE
    if store.delivery_notes():
        yield EvidenceKind.DELIVERY_NOTE
    if store.ledger():
        yield EvidenceKind.LEDGER
    if store.stock_counts():
        yield EvidenceKind.STOCK_COUNT
    if store.payments():
        yield EvidenceKind.PAYMENT
