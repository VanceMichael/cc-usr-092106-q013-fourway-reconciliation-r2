"""四向匹配引擎。

引擎只做两件事：
  1. 沿采购关系提出匹配候选（哪张票配哪些同行单、台账行、付款）；
  2. 把对不上的地方按缺项、数量差、日期冲突、主体不一致四类分开列出。

引擎不输出“违规”结论。分批送货、赊销、退货、折让、合并付款属于正常
业务，能被这些单据解释的差异不产生线索；只能部分解释的，在线索上
注明已解释部分，由检查人员定夺。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .loader import EvidenceBundle
from .models import (
    Allowance,
    EvidenceKind,
    Invoice,
    Payment,
    Relation,
    ReturnNote,
)
from .store import EvidenceStore


class DifferenceCategory(str, Enum):
    MISSING = "missing"      # 缺项：四向中某一向没有对应材料
    QUANTITY = "quantity"    # 数量差：票/货/账/实/款对不上
    DATE = "date"            # 日期冲突：先后关系或有效期矛盾
    PARTY = "party"          # 主体不一致：与采购关系登记的主体不符


@dataclass(frozen=True)
class EvidenceRef:
    """指向某一版原始依据，溯源时按 kind + ref_id + version 取回。"""

    kind: EvidenceKind
    ref_id: str
    version: int | None = None


@dataclass(frozen=True)
class Difference:
    """一条待人工核实的线索，不是违规结论。"""

    difference_id: str
    category: DifferenceCategory
    summary: str
    refs: tuple[EvidenceRef, ...]
    relation_id: str | None = None
    explained_by: tuple[str, ...] = ()  # 已部分解释差异的正常业务单据


@dataclass(frozen=True)
class MatchCandidate:
    """按采购关系提出的一组候选对应关系。"""

    relation_id: str
    invoice_key: str          # 发票版本键，如 INV-1@v2
    delivery_note_nos: tuple[str, ...]
    ledger_entry_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]


@dataclass(frozen=True)
class TemplateCluster:
    """同一票据模板在不同门店/供应商处反复出现。"""

    template_id: str
    supplier_ids: tuple[str, ...]
    pharmacy_ids: tuple[str, ...]
    invoice_keys: tuple[str, ...]


@dataclass
class ReconciliationReport:
    as_of: date
    candidates: list[MatchCandidate] = field(default_factory=list)
    differences: list[Difference] = field(default_factory=list)
    template_clusters: list[TemplateCluster] = field(default_factory=list)

    def by_category(self) -> dict[DifferenceCategory, list[Difference]]:
        grouped: dict[DifferenceCategory, list[Difference]] = {
            c: [] for c in DifferenceCategory
        }
        for diff in self.differences:
            grouped[diff.category].append(diff)
        return grouped


class _UnionFind:
    """把发票与付款按引用关系连成组件，处理合并付款与分批付款。"""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def reconcile(
    bundle: EvidenceBundle, store: EvidenceStore, as_of: date
) -> ReconciliationReport:
    engine = _Engine(bundle, store, as_of)
    return engine.run()


class _Engine:
    def __init__(
        self, bundle: EvidenceBundle, store: EvidenceStore, as_of: date
    ) -> None:
        self.bundle = bundle
        self.store = store
        self.as_of = as_of
        self.report = ReconciliationReport(as_of=as_of)
        self._seq = 0

    # ---- 工具 ----

    def _emit(
        self,
        category: DifferenceCategory,
        summary: str,
        refs: list[EvidenceRef],
        relation_id: str | None = None,
        explained_by: tuple[str, ...] = (),
    ) -> None:
        self._seq += 1
        self.report.differences.append(
            Difference(
                difference_id=f"D{self._seq:04d}",
                category=category,
                summary=summary,
                refs=tuple(refs),
                relation_id=relation_id,
                explained_by=explained_by,
            )
        )

    def _relation_for(self, invoice: Invoice) -> Relation | None:
        for rel in self.bundle.relations:
            if (
                rel.supplier_id == invoice.supplier_id
                and rel.pharmacy_id == invoice.pharmacy_id
            ):
                return rel
        return None

    # ---- 主流程 ----

    def run(self) -> ReconciliationReport:
        invoices = self.store.invoices()  # 每票取最新版本
        for invoice in invoices:
            self._check_invoice(invoice)
        self._check_payments(invoices)
        self._check_stock()
        self._find_template_clusters(invoices)
        return self.report

    def _check_invoice(self, invoice: Invoice) -> None:
        inv_ref = EvidenceRef(EvidenceKind.INVOICE, invoice.invoice_id, invoice.version)
        relation = self._relation_for(invoice)
        relation_id = relation.relation_id if relation else None

        # 主体一致性：票上双方必须对应一条登记在册的采购关系
        if relation is None:
            self._emit(
                DifferenceCategory.PARTY,
                f"发票 {invoice.invoice_id} 的供需双方 "
                f"{invoice.supplier_id}→{invoice.pharmacy_id} 无登记采购关系",
                [inv_ref],
            )
        elif not relation.covers(invoice.issue_date):
            self._emit(
                DifferenceCategory.DATE,
                f"发票 {invoice.invoice_id} 开票日 {invoice.issue_date} "
                f"不在采购关系 {relation.relation_id} 有效期内",
                [inv_ref],
                relation_id,
            )

        # 票↔货：同行单数量合计（分批送货求和）对比票面
        notes = self.store.delivery_notes(invoice.invoice_id)
        note_refs = [EvidenceRef(EvidenceKind.DELIVERY_NOTE, n.note_no) for n in notes]
        if not notes:
            self._emit(
                DifferenceCategory.MISSING,
                f"发票 {invoice.invoice_id} 缺随货同行单",
                [inv_ref],
                relation_id,
            )
        else:
            for note in notes:
                if note.ship_date < invoice.issue_date:
                    self._emit(
                        DifferenceCategory.DATE,
                        f"随货同行单 {note.note_no} 发货日 {note.ship_date} "
                        f"早于发票 {invoice.invoice_id} 开票日 {invoice.issue_date}",
                        [inv_ref, EvidenceRef(EvidenceKind.DELIVERY_NOTE, note.note_no)],
                        relation_id,
                    )
                if (
                    note.supplier_id != invoice.supplier_id
                    or note.pharmacy_id != invoice.pharmacy_id
                ):
                    self._emit(
                        DifferenceCategory.PARTY,
                        f"随货同行单 {note.note_no} 主体与发票 {invoice.invoice_id} 不一致",
                        [inv_ref, EvidenceRef(EvidenceKind.DELIVERY_NOTE, note.note_no)],
                        relation_id,
                    )
            self._compare_invoice_vs_notes(invoice, notes, inv_ref, note_refs, relation_id)

        # 货↔账：台账入库应等于交付减退货
        self._compare_notes_vs_ledger(invoice, notes, inv_ref, note_refs, relation_id)

        # 候选：把这组对应关系记下来
        ledger_ids = tuple(
            e.entry_id
            for e in self.store.ledger()
            if e.source_doc == invoice.invoice_id
            or e.source_doc in {n.note_no for n in notes}
        )
        payment_ids = tuple(
            p.payment_id for p in self.store.payments() if invoice.invoice_id in p.invoice_ids
        )
        if relation is not None:
            self.report.candidates.append(
                MatchCandidate(
                    relation_id=relation.relation_id,
                    invoice_key=invoice.version_key,
                    delivery_note_nos=tuple(n.note_no for n in notes),
                    ledger_entry_ids=ledger_ids,
                    payment_ids=payment_ids,
                )
            )

    def _compare_invoice_vs_notes(
        self,
        invoice: Invoice,
        notes,
        inv_ref: EvidenceRef,
        note_refs: list[EvidenceRef],
        relation_id: str | None,
    ) -> None:
        delivered: dict[str, int] = {}
        for note in notes:
            for line in note.lines:
                delivered[line.product_id] = (
                    delivered.get(line.product_id, 0) + line.qty.value
                )
        for line in invoice.lines:
            got = delivered.get(line.product_id, 0)
            want = line.qty.value
            if got != want:
                self._emit(
                    DifferenceCategory.QUANTITY,
                    f"发票 {invoice.invoice_id} 商品 {line.product_id} "
                    f"票面 {want}，同行单累计交付 {got}"
                    + ("，可能存在在途分批" if got < want else ""),
                    [inv_ref, *note_refs],
                    relation_id,
                )

    def _compare_notes_vs_ledger(
        self,
        invoice: Invoice,
        notes,
        inv_ref: EvidenceRef,
        note_refs: list[EvidenceRef],
        relation_id: str | None,
    ) -> None:
        if not notes:
            return
        doc_nos = {n.note_no for n in notes} | {invoice.invoice_id}
        returns_for_invoice = [
            n for n in self.store.returns() if n.invoice_id == invoice.invoice_id
        ]
        doc_nos |= {r.return_id for r in returns_for_invoice}
        inbound: dict[str, int] = {}
        ledger_refs: list[EvidenceRef] = []
        for entry in self.store.ledger():
            if entry.pharmacy_id != invoice.pharmacy_id or entry.source_doc not in doc_nos:
                continue
            ledger_refs.append(EvidenceRef(EvidenceKind.LEDGER, entry.entry_id))
            inbound[entry.product_id] = inbound.get(entry.product_id, 0) + entry.change
            first_ship = min(n.ship_date for n in notes)
            if entry.change > 0 and entry.entry_date < first_ship:
                self._emit(
                    DifferenceCategory.DATE,
                    f"台账 {entry.entry_id} 入库日 {entry.entry_date} "
                    f"早于最早发货日 {first_ship}",
                    [EvidenceRef(EvidenceKind.LEDGER, entry.entry_id), *note_refs],
                    relation_id,
                )

        returned: dict[str, int] = {}
        return_ids: list[str] = []
        for note in returns_for_invoice:
            returned[note.product_id] = (
                returned.get(note.product_id, 0) + note.qty.value
            )
            return_ids.append(note.return_id)

        delivered: dict[str, int] = {}
        for note in notes:
            for line in note.lines:
                delivered[line.product_id] = (
                    delivered.get(line.product_id, 0) + line.qty.value
                )

        if not ledger_refs:
            self._emit(
                DifferenceCategory.MISSING,
                f"发票 {invoice.invoice_id} 对应交付缺台账入库记录",
                [inv_ref, *note_refs],
                relation_id,
            )
            return

        for pid, sent in delivered.items():
            expect = sent - returned.get(pid, 0)
            got = inbound.get(pid, 0)
            if got == expect:
                continue
            explained = tuple(f"退货单 {rid}" for rid in return_ids)
            self._emit(
                DifferenceCategory.QUANTITY,
                f"发票 {invoice.invoice_id} 商品 {pid} 交付 {sent}"
                + (f"，退货 {returned[pid]}" if returned.get(pid) else "")
                + f"，台账入库 {got}",
                [inv_ref, *note_refs, *ledger_refs],
                relation_id,
                explained_by=explained,
            )

    def _check_payments(self, invoices: list[Invoice]) -> None:
        by_id = {inv.invoice_id: inv for inv in invoices}
        allowances: dict[str, list[Allowance]] = {}
        for alw in self.store.allowances():
            allowances.setdefault(alw.invoice_id, []).append(alw)

        uf = _UnionFind()
        payments = self.store.payments()
        for payment in payments:
            uf.union(f"pay:{payment.payment_id}", f"pay:{payment.payment_id}")
            for inv_id in payment.invoice_ids:
                uf.union(f"pay:{payment.payment_id}", f"inv:{inv_id}")
        for inv_id in by_id:
            uf.find(f"inv:{inv_id}")

        components: dict[str, dict[str, list]] = {}
        for payment in payments:
            comp = components.setdefault(
                uf.find(f"pay:{payment.payment_id}"), {"payments": [], "invoices": set()}
            )
            comp["payments"].append(payment)
            comp["invoices"].update(payment.invoice_ids)
        for inv_id in by_id:
            root = uf.find(f"inv:{inv_id}")
            comp = components.setdefault(root, {"payments": [], "invoices": set()})
            comp["invoices"].add(inv_id)

        paid_invoice_ids: set[str] = {
            inv_id for payment in payments for inv_id in payment.invoice_ids
        }
        for comp in components.values():
            comp_invoices = [by_id[i] for i in comp["invoices"] if i in by_id]
            comp_payments: list[Payment] = comp["payments"]

            expected = 0
            explained: list[str] = []
            for inv in comp_invoices:
                expected += inv.total_fen
                for alw in allowances.get(inv.invoice_id, []):
                    expected -= alw.amount_fen
                    explained.append(f"折让单 {alw.allowance_id}")
            paid = sum(p.amount_fen for p in comp_payments)

            refs = [
                EvidenceRef(EvidenceKind.INVOICE, inv.invoice_id, inv.version)
                for inv in comp_invoices
            ] + [
                EvidenceRef(EvidenceKind.PAYMENT, p.payment_id) for p in comp_payments
            ]
            relation_id = None
            if comp_invoices:
                rel = self._relation_for(comp_invoices[0])
                relation_id = rel.relation_id if rel else None

            for payment in comp_payments:
                self._check_payment_parties_and_dates(
                    payment, comp_invoices, refs, relation_id
                )

            if not comp_payments:
                continue  # 完全未付的发票在下方按账期判断
            if not comp_invoices:
                continue  # 未注明发票的付款已在下方报缺项，不重复报金额差
            if paid != expected:
                diff = paid - expected
                direction = "少付" if diff < 0 else "多付"
                inv_list = "、".join(sorted(comp["invoices"]))
                summary = (
                    f"付款合计 {paid} 分与应付 {expected} 分{direction} {abs(diff)} 分"
                    f"（涉及发票 {inv_list}）"
                )
                self._emit(
                    DifferenceCategory.QUANTITY,
                    summary,
                    refs,
                    relation_id,
                    explained_by=tuple(explained),
                )

        # 完全未付：赊销在账期内不算缺项
        for inv in invoices:
            if inv.invoice_id in paid_invoice_ids:
                continue
            relation = self._relation_for(inv)
            relation_id = relation.relation_id if relation else None
            inv_ref = EvidenceRef(EvidenceKind.INVOICE, inv.invoice_id, inv.version)
            if inv.payment_due is not None and self.as_of <= inv.payment_due:
                continue  # 赊销账期内
            due_text = f"，约定付款日 {inv.payment_due} 已过" if inv.payment_due else ""
            self._emit(
                DifferenceCategory.MISSING,
                f"发票 {inv.invoice_id} 未见付款{due_text}",
                [inv_ref],
                relation_id,
            )

        # 未注明发票的付款：无法并入任何组件
        for payment in payments:
            if not payment.invoice_ids:
                self._emit(
                    DifferenceCategory.MISSING,
                    f"付款 {payment.payment_id} 未注明对应发票，无法核对",
                    [EvidenceRef(EvidenceKind.PAYMENT, payment.payment_id)],
                )

    def _check_payment_parties_and_dates(
        self,
        payment: Payment,
        invoices: list[Invoice],
        refs: list[EvidenceRef],
        relation_id: str | None,
    ) -> None:
        pay_ref = EvidenceRef(EvidenceKind.PAYMENT, payment.payment_id)
        for inv in invoices:
            if payment.payer_id != inv.pharmacy_id or payment.payee_id != inv.supplier_id:
                self._emit(
                    DifferenceCategory.PARTY,
                    f"付款 {payment.payment_id} 收付双方 "
                    f"{payment.payer_id}→{payment.payee_id} 与发票 {inv.invoice_id} 主体不符",
                    [pay_ref, EvidenceRef(EvidenceKind.INVOICE, inv.invoice_id, inv.version)],
                    relation_id,
                )
            if payment.pay_date < inv.issue_date:
                self._emit(
                    DifferenceCategory.DATE,
                    f"付款 {payment.payment_id} 付款日 {payment.pay_date} "
                    f"早于发票 {inv.invoice_id} 开票日 {inv.issue_date}",
                    [pay_ref, EvidenceRef(EvidenceKind.INVOICE, inv.invoice_id, inv.version)],
                    relation_id,
                )

    def _check_stock(self) -> None:
        """账↔实：台账滚动余额对比现场清点。期初未知时以期初为零口径。"""
        for count in self.store.stock_counts():
            balance = 0
            refs = [EvidenceRef(EvidenceKind.STOCK_COUNT, count.count_id)]
            for entry in self.store.ledger():
                if (
                    entry.pharmacy_id == count.pharmacy_id
                    and entry.product_id == count.product_id
                    and entry.entry_date <= count.count_date
                ):
                    balance += entry.change
                    refs.append(EvidenceRef(EvidenceKind.LEDGER, entry.entry_id))
            if balance != count.qty.value:
                self._emit(
                    DifferenceCategory.QUANTITY,
                    f"药店 {count.pharmacy_id} 商品 {count.product_id} "
                    f"账面 {balance}，实盘 {count.qty.value}",
                    refs,
                )

    def _find_template_clusters(self, invoices: list[Invoice]) -> None:
        by_template: dict[str, list[Invoice]] = {}
        for inv in invoices:
            if inv.template_id:
                by_template.setdefault(inv.template_id, []).append(inv)
        for template_id, group in by_template.items():
            pharmacies = sorted({inv.pharmacy_id for inv in group})
            suppliers = sorted({inv.supplier_id for inv in group})
            if len(pharmacies) < 2:
                continue
            self.report.template_clusters.append(
                TemplateCluster(
                    template_id=template_id,
                    supplier_ids=tuple(suppliers),
                    pharmacy_ids=tuple(pharmacies),
                    invoice_keys=tuple(sorted(inv.version_key for inv in group)),
                )
            )
