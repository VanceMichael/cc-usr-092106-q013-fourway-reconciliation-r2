"""四向核验匹配引擎。

输入为 SubmissionRegistry 的最新有效版本，输出按采购关系组织的匹配候选与差异。

铁律：引擎只产出候选、差异和常见商业情形标注，绝不输出"违规"结论。
差异固定分为四类：missing（缺项）/ quantity（数量金额差）/
date（日期冲突）/ party（主体不一致）。分批送货、赊销、退货、折让、
合并付款以 explainable_pattern 标注为常见商业情形，状态为 open（待人工确认），
不进入违规暗示。

实物口径：进销存片段覆盖期初至清点日的，按
  应有库存 = 购入入账 − 购退出账 − 销售出账（按品种+批号）
与现场清点 total_qty 比较；片段未覆盖该门店的，只给覆盖性提示，不判缺项。
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .registry import MaterialRef, SubmissionRegistry

# 类别常量
MISSING = "missing"
QUANTITY = "quantity"
DATE = "date"
PARTY = "party"

AMOUNT_TOLERANCE = Decimal("0.01")
DELIVERY_BEFORE_TOLERANCE = timedelta(days=3)
LEDGER_DATE_TOLERANCE = timedelta(days=7)


@dataclass
class Difference:
    diff_id: str
    category: str
    kind: str
    message: str
    evidence: list[MaterialRef]
    explainable_pattern: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def benign_pattern(self) -> bool:
        """属于题述五种常见商业情形之一，不得被解读为必然违规。"""
        return self.explainable_pattern is not None

    def as_dict(self) -> dict:
        return {
            "diff_id": self.diff_id,
            "category": self.category,
            "kind": self.kind,
            "explainable_pattern": self.explainable_pattern,
            "message": self.message,
            "detail": self.detail,
            "evidence": [r.as_dict() for r in self.evidence],
        }


def _d(value) -> Decimal:
    return Decimal(str(value))


def parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])


def _invoice_payable(invoice: dict) -> Decimal:
    """应付 = 价税合计（已扣折让）− 退货金额。"""
    returned = sum((_d(r["amount"]) for r in invoice.get("return_documents", [])), Decimal("0"))
    return _d(invoice["total_amount"]) - returned


def _delivery_qty(invoice: dict) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for note in invoice.get("delivery_notes", []):
        for line in note["lines"]:
            totals[line["product_code"]] += _d(line["qty"])
    return totals


def _delivery_batches(invoice: dict) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for note in invoice.get("delivery_notes", []):
        for line in note["lines"]:
            totals[(line["product_code"], line["batch_no"])] += _d(line["qty"])
    return totals


def _return_batches(invoice: dict) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for doc in invoice.get("return_documents", []):
        for line in doc["lines"]:
            totals[(line["product_code"], line["batch_no"])] += _d(line["qty"])
    return totals


class Reconciliation:
    def __init__(self, registry: SubmissionRegistry, case_id: str, as_of: str) -> None:
        self.registry = registry
        self.case_id = case_id
        self.as_of = parse_day(as_of)
        self.differences: list[Difference] = []
        self._diff_seq = 0

    def _add_diff(self, category, kind, message, evidence, pattern=None, detail=None) -> Difference:
        self._diff_seq += 1
        diff = Difference(
            diff_id=f"diff-{self.case_id}-{self._diff_seq:04d}",
            category=category,
            kind=kind,
            message=message,
            evidence=evidence,
            explainable_pattern=pattern,
            detail=detail or {},
        )
        self.differences.append(diff)
        return diff

    def run(self) -> dict:
        invoices = [
            e for e in self.registry.latest_all("invoice_pack")
            if e["case_id"] == self.case_id
        ]
        ledgers = [
            e for e in self.registry.latest_all("ledger_snippet")
            if e["case_id"] == self.case_id
        ]
        counts = [
            e for e in self.registry.latest_all("stock_count")
            if e["case_id"] == self.case_id
        ]
        payments = [
            e for e in self.registry.latest_all("payment_summary")
            if e["case_id"] == self.case_id
        ]

        allocation_index = self._index_payments(payments)
        claimed_txn: set[str] = set()
        claimed_ledger: set[str] = set()
        candidates = []

        for envelope in sorted(invoices, key=lambda e: e["content"]["invoice_date"]):
            candidates.append(
                self._match_invoice(
                    envelope, ledgers, counts, allocation_index,
                    claimed_txn, claimed_ledger,
                )
            )

        unmatched = self._collect_unmatched(ledgers, counts, payments, invoices,
                                            claimed_ledger, claimed_txn)
        coverage = self._coverage_warnings(invoices, ledgers, counts)

        return {
            "case_id": self.case_id,
            "as_of": self.as_of.isoformat(),
            "candidates": candidates,
            "unmatched": unmatched,
            "coverage_warnings": coverage,
        }

    # ------------------------------------------------------------------ 索引

    def _index_payments(self, payments: list[dict]) -> dict[str, list[dict]]:
        """发票号 -> [(信封, 交易, 分摊金额)]，合并付款天然支持。"""
        index: dict[str, list[dict]] = defaultdict(list)
        for envelope in payments:
            for txn in envelope["content"]["transactions"]:
                for alloc in txn["allocations"]:
                    index[alloc["invoice_number"]].append(
                        {"envelope": envelope, "txn": txn, "amount": _d(alloc["amount"])}
                    )
        return index

    # ----------------------------------------------------------- 单票匹配

    def _match_invoice(self, envelope, ledgers, counts, allocation_index,
                       claimed_txn, claimed_ledger) -> dict:
        before = len(self.differences)
        inv = envelope["content"]
        ref = self.registry.ref_of(envelope)
        seller_id = inv["seller"]["party_id"]
        invoice_day = parse_day(inv["invoice_date"])
        due_day = invoice_day + timedelta(days=inv["payment_terms_days"])
        line_qty = {l["product_code"]: _d(l["qty"]) for l in inv["lines"]}

        # 一、票面内部金额（折让后）自洽
        self._check_invoice_amounts(envelope)

        # 二、票 vs 随货同行单（分批送货），同时产出分批缺口差异
        self._check_delivery(envelope, line_qty, invoice_day)

        # 三、票/单 vs 进销存（含退货）
        ledger_state = self._check_ledger(
            envelope, ledgers, claimed_ledger,
            invoice_day, seller_id, counts,
        )

        # 四、账 vs 实物
        self._check_stock(envelope, counts, ledger_state)

        # 五、票 vs 款（赊销、合并付款、主体）
        self._check_payment(envelope, allocation_index, claimed_txn, due_day)

        diffs = [d.as_dict() for d in self.differences[before:]]
        open_patterns = {d.explainable_pattern for d in self.differences[before:]
                         if d.explainable_pattern}
        if not diffs:
            status = "reconciled"
        elif open_patterns and all(d.benign_pattern for d in self.differences[before:]):
            status = "open"
        else:
            status = "review"

        return {
            "candidate_id": f"cand-{inv['invoice_number']}",
            "relationship": {
                "supplier_id": seller_id,
                "supplier_name": inv["seller"]["name"],
                "pharmacy_id": inv["pharmacy_id"],
                "store_id": inv["store_id"],
            },
            "invoice_number": inv["invoice_number"],
            "invoice_ref": ref.as_dict(),
            "seal_facts": {
                # 仅记录票面事实，不参与任何差异计算
                "has_copy_seal": inv["has_copy_seal"],
                "has_original_seal": inv["has_original_seal"],
            },
            "invoice_date": inv["invoice_date"],
            "due_date": due_day.isoformat(),
            "payable_amount": str(_invoice_payable(inv)),
            "status": status,
            "differences": diffs,
        }

    def _check_invoice_amounts(self, envelope) -> None:
        inv = envelope["content"]
        ref = self.registry.ref_of(envelope)
        for line in inv["lines"]:
            discount = _d(line.get("discount_amount", 0))
            expected = (_d(line["qty"]) * _d(line["unit_price"]) - discount).quantize(Decimal("0.01"))
            actual = _d(line["amount"]).quantize(Decimal("0.01"))
            if abs(expected - actual) > AMOUNT_TOLERANCE:
                self._add_diff(
                    QUANTITY, "line_amount_mismatch",
                    f"发票 {inv['invoice_number']} 第{line['line_no']}行金额"
                    f"{actual} 与数量单价折让推算 {expected} 不一致",
                    [ref],
                    detail={"product_code": line["product_code"],
                            "expected": str(expected), "actual": str(actual)},
                )
        total = sum((_d(l["amount"]) for l in inv["lines"]), Decimal("0")).quantize(Decimal("0.01"))
        if abs(total - _d(inv["total_amount"])) > AMOUNT_TOLERANCE:
            self._add_diff(
                QUANTITY, "invoice_total_mismatch",
                f"发票 {inv['invoice_number']} 价税合计 {inv['total_amount']}"
                f" 与各行金额合计 {total} 不一致（折让应体现在行内）",
                [ref],
            )

    def _check_delivery(self, envelope, line_qty, invoice_day) -> dict[str, Decimal]:
        inv = envelope["content"]
        ref = self.registry.ref_of(envelope)
        notes = inv.get("delivery_notes", [])
        if not notes:
            self._add_diff(
                MISSING, "delivery_note_missing",
                f"发票 {inv['invoice_number']} 未附随货同行单",
                [ref],
            )
            return {}

        delivery_qty = _delivery_qty(inv)
        note_days = []
        for note in notes:
            day = parse_day(note["note_date"])
            note_days.append(day)
            if day < invoice_day - DELIVERY_BEFORE_TOLERANCE:
                self._add_diff(
                    DATE, "delivery_before_invoice",
                    f"随货同行单 {note['note_number']} 日期 {note['note_date']}"
                    f" 早于发票日期 {inv['invoice_date']} 超过容差",
                    [ref],
                )

        for code, billed in line_qty.items():
            delivered = delivery_qty.get(code, Decimal("0"))
            if delivered < billed:
                gap = billed - delivered
                self._add_diff(
                    QUANTITY, "partial_delivery",
                    f"发票 {inv['invoice_number']} 品种 {code} 开票 {billed}，"
                    f"随货同行累计 {delivered}，尚有 {gap} 未交付（分批送货）",
                    [ref],
                    pattern="partial_delivery",
                    detail={"product_code": code, "billed": str(billed),
                            "delivered": str(delivered), "gap": str(gap)},
                )
            elif delivered > billed:
                self._add_diff(
                    QUANTITY, "delivery_excess",
                    f"发票 {inv['invoice_number']} 品种 {code} 随货同行累计 "
                    f"{delivered} 超过开票 {billed}",
                    [ref],
                    detail={"product_code": code, "billed": str(billed),
                            "delivered": str(delivered)},
                )
        return delivery_qty

    def _store_snippets(self, ledgers, inv):
        return [e for e in ledgers
                if e["content"]["store_id"] == inv["store_id"]
                and e["content"]["pharmacy_id"] == inv["pharmacy_id"]]

    def _check_ledger(self, envelope, ledgers, claimed_ledger,
                      invoice_day, seller_id, counts) -> dict:
        inv = envelope["content"]
        ref = self.registry.ref_of(envelope)
        snippets = self._store_snippets(ledgers, inv)
        count_day = None
        for c in counts:
            if c["content"]["store_id"] == inv["store_id"]:
                count_day = parse_day(c["content"]["counted_at"])
                break

        # 返回按品种+批号汇总
        returned = _return_batches(inv)
        purchases: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        returns_out: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        sales_out: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        # 供库存推算的带日期明细
        purchase_rows: list[tuple[tuple[str, str], Decimal, object]] = []
        return_rows: list[tuple[tuple[str, str], Decimal, object]] = []
        sale_rows: list[tuple[tuple[str, str], Decimal, object]] = []
        matched_entries = []
        evidence = [ref]

        if not snippets:
            # 覆盖性问题在 coverage_warnings 统一提示，这里不按缺项误报
            return {"purchases": purchases, "returns": returns_out,
                    "sales": sales_out, "snippets": [],
                    "purchase_rows": purchase_rows, "return_rows": return_rows,
                    "sale_rows": sale_rows,
                    "covered": False, "matched_entries": []}

        for snip_env in snippets:
            snip = snip_env["content"]
            evidence.append(self.registry.ref_of(snip_env))
            for entry in snip["entries"]:
                key = (entry["product_code"], entry["batch_no"])
                if entry["direction"] == "purchase_in":
                    strong = inv["invoice_number"] in entry.get("ref_numbers", [])
                    weak = (
                        entry.get("supplier_id") == seller_id
                        and key in _delivery_batches(inv)
                        and invoice_day - LEDGER_DATE_TOLERANCE
                        <= parse_day(entry["entry_date"])
                        <= self.as_of
                    )
                    if (strong or weak) and entry["entry_id"] not in claimed_ledger:
                        claimed_ledger.add(entry["entry_id"])
                        matched_entries.append(entry)
                        purchases[key] += _d(entry["qty"])
                        purchase_rows.append((key, _d(entry["qty"]),
                                              parse_day(entry["entry_date"])))
                        if not strong:
                            self._add_diff(
                                MISSING, "ledger_ref_missing",
                                f"台账入账 {entry['entry_id']} 数量与送货相符，"
                                f"但 ref_numbers 未引用发票 {inv['invoice_number']}",
                                [ref, self.registry.ref_of(snip_env)],
                                detail=dict(entry_id=entry["entry_id"]),
                            )
                        if entry.get("supplier_id") and entry["supplier_id"] != seller_id:
                            self._add_diff(
                                PARTY, "ledger_supplier_mismatch",
                                f"台账 {entry['entry_id']} 供应商 {entry['supplier_id']}"
                                f" 与发票销方 {seller_id} 不一致",
                                [ref, self.registry.ref_of(snip_env)],
                            )
                elif entry["direction"] == "return_out":
                    refs = entry.get("ref_numbers", [])
                    if any(rd["doc_number"] in refs for rd in inv.get("return_documents", [])) \
                            or (key in returned and entry.get("supplier_id") == seller_id):
                        claimed_ledger.add(entry["entry_id"])
                        returns_out[key] += _d(entry["qty"])
                        return_rows.append((key, _d(entry["qty"]),
                                            parse_day(entry["entry_date"])))
                elif entry["direction"] == "sale_out":
                    # 只扣减清点日（含）之前的销售
                    if count_day is None or parse_day(entry["entry_date"]) <= count_day:
                        sales_out[key] += _d(entry["qty"])
                        sale_rows.append((key, _d(entry["qty"]),
                                          parse_day(entry["entry_date"])))

        # 票/单数量 vs 台账入账（按品种汇总批号）
        delivered_by_product: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for (code, _batch), qty in _delivery_batches(inv).items():
            delivered_by_product[code] += qty
        booked_by_product: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for (code, _batch), qty in purchases.items():
            booked_by_product[code] += qty

        if not matched_entries:
            # 票有账无：无论同行单是否齐备，开票行都应有购入入账
            for line in inv["lines"]:
                self._add_diff(
                    MISSING, "ledger_inbound_missing",
                    f"发票 {inv['invoice_number']} 品种 {line['product_code']}"
                    f" 在台账中无购入入账",
                    evidence,
                    detail={"product_code": line["product_code"]},
                )
        else:
            for code, billed in {l["product_code"]: _d(l["qty"]) for l in inv["lines"]}.items():
                booked = booked_by_product.get(code, Decimal("0"))
                delivered = delivered_by_product.get(code, Decimal("0")) or billed
                if booked == 0:
                    self._add_diff(
                        MISSING, "ledger_inbound_missing",
                        f"发票 {inv['invoice_number']} 品种 {code} 在台账中无购入入账",
                        evidence,
                        detail={"product_code": code},
                    )
                elif booked < delivered:
                    self._add_diff(
                        QUANTITY, "ledger_inbound_short",
                        f"品种 {code} 随货同行 {delivered}，台账购入入账 {booked}",
                        evidence,
                        detail={"product_code": code, "delivered": str(delivered),
                                "booked": str(booked)},
                    )
                elif booked > delivered:
                    self._add_diff(
                        QUANTITY, "ledger_inbound_excess",
                        f"品种 {code} 台账购入入账 {booked}，多于随货同行 {delivered}",
                        evidence,
                        detail={"product_code": code, "delivered": str(delivered),
                                "booked": str(booked)},
                    )

        # 入账日期窗口
        last_delivery = max(
            (parse_day(n["note_date"]) for n in inv.get("delivery_notes", [])),
            default=invoice_day,
        )
        for entry in matched_entries:
            day = parse_day(entry["entry_date"])
            if day < invoice_day - LEDGER_DATE_TOLERANCE or day > last_delivery + LEDGER_DATE_TOLERANCE:
                self._add_diff(
                    DATE, "ledger_date_out_of_window",
                    f"台账入账 {entry['entry_id']} 日期 {entry['entry_date']}"
                    f" 超出票货日期窗口",
                    evidence,
                    detail={"entry_id": entry["entry_id"], "entry_date": entry["entry_date"]},
                )

        # 退货凭证 vs 台账购退出账
        for doc in inv.get("return_documents", []):
            doc_ref = ref
            for line in doc["lines"]:
                key = (line["product_code"], line["batch_no"])
                booked_ret = returns_out.get(key, Decimal("0"))
                doc_qty = _d(line["qty"])
                if booked_ret == 0:
                    self._add_diff(
                        MISSING, "ledger_return_missing",
                        f"退货凭证 {doc['doc_number']} 品种 {line['product_code']}"
                        f" 批号 {line['batch_no']} 在台账无购退出账",
                        [doc_ref] + [self.registry.ref_of(s) for s in snippets],
                        pattern="return",
                    )
                elif booked_ret < doc_qty:
                    self._add_diff(
                        QUANTITY, "ledger_return_short",
                        f"退货凭证 {doc['doc_number']} 数量 {doc_qty}，"
                        f"台账购退出账 {booked_ret}",
                        [doc_ref] + [self.registry.ref_of(s) for s in snippets],
                        pattern="return",
                    )

        return {"purchases": purchases, "returns": returns_out,
                "sales": sales_out, "snippets": snippets,
                "purchase_rows": purchase_rows, "return_rows": return_rows,
                "sale_rows": sale_rows,
                "covered": True, "matched_entries": matched_entries}

    def _check_stock(self, envelope, counts, ledger_state) -> None:
        inv = envelope["content"]
        ref = self.registry.ref_of(envelope)
        if not ledger_state["covered"]:
            return
        store_counts = [e for e in counts if e["content"]["store_id"] == inv["store_id"]]
        if not store_counts:
            # 与 coverage_warnings 的 stock_coverage_gap 同源，避免重复上报
            return

        count_env = store_counts[0]
        count = count_env["content"]
        count_ref = self.registry.ref_of(count_env)
        count_index = {(i["product_code"], i["batch_no"]): i for i in count["items"]}
        delivered_batches = _delivery_batches(inv)
        returned_batches = _return_batches(inv)

        # 只在本发票实际送货（及本票退货）的批号范围内推算应有库存，
        # 片段内其他采购关系的购销记录不得串到本票；购入/退货/销售均截止清点日。
        keys = set(delivered_batches) | set(returned_batches)
        expected: dict[tuple[str, str], Decimal] = {}
        for key in keys:
            inbound = sum(q for k, q, d in ledger_state["purchase_rows"]
                          if k == key and d <= parse_day(count["counted_at"]))
            outbound_return = sum(q for k, q, d in ledger_state["return_rows"]
                                  if k == key and d <= parse_day(count["counted_at"]))
            outbound_sale = sum(q for k, q, d in ledger_state["sale_rows"]
                                if k == key and d <= parse_day(count["counted_at"]))
            expected[key] = inbound - outbound_return - outbound_sale

        for key in keys:
            code, batch = key
            should_be = expected.get(key, Decimal("0"))
            item = count_index.get(key)
            if item is None:
                if should_be > 0:
                    self._add_diff(
                        MISSING, "stock_item_missing",
                        f"现场清点未见品种 {code} 批号 {batch}（应有 {should_be}）",
                        [ref, count_ref],
                        detail={"product_code": code, "batch_no": batch,
                                "expected": str(should_be)},
                    )
                continue
            actual = _d(item["total_qty"])
            # 箱盒换算自洽
            calc = _d(item["box_spec"]) * item["box_count"] + _d(item["loose_qty"])
            if calc != actual:
                self._add_diff(
                    QUANTITY, "box_count_mismatch",
                    f"品种 {code} 批号 {batch} 清点 total_qty {actual} 与"
                    f" 箱规 {item['box_spec']}×{item['box_count']}+"
                    f"{item['loose_qty']}={calc} 不符",
                    [count_ref],
                )
            if actual != should_be:
                self._add_diff(
                    QUANTITY, "stock_variance",
                    f"品种 {code} 批号 {batch} 应有库存 {should_be}，"
                    f"现场清点 {actual}，差额 {actual - should_be}",
                    [ref, count_ref],
                    detail={"product_code": code, "batch_no": batch,
                            "expected": str(should_be), "actual": str(actual)},
                )

    def _check_payment(self, envelope, allocation_index, claimed_txn, due_day) -> None:
        inv = envelope["content"]
        ref = self.registry.ref_of(envelope)
        payable = _invoice_payable(inv)
        allocs = allocation_index.get(inv["invoice_number"], [])
        paid = sum((a["amount"] for a in allocs), Decimal("0"))

        if not allocs:
            if self.as_of <= due_day:
                self._add_diff(
                    MISSING, "payment_not_due",
                    f"发票 {inv['invoice_number']} 尚无付款，账期至 {due_day}"
                    f"（赊销期内，非违规）",
                    [ref],
                    pattern="credit_sale",
                )
            else:
                self._add_diff(
                    MISSING, "payment_unpaid",
                    f"发票 {inv['invoice_number']} 账期至 {due_day}，"
                    f"截至 {self.as_of} 未收到付款（赊销逾期，待人工认定）",
                    [ref],
                    pattern="credit_sale",
                )
            return

        evidence = [ref]
        for a in allocs:
            claimed_txn.add(a["txn"]["txn_id"])
            evidence.append(self.registry.ref_of(a["envelope"]))
            txn = a["txn"]
            pay_day = parse_day(txn["txn_date"])
            if pay_day > due_day:
                self._add_diff(
                    DATE, "payment_late",
                    f"发票 {inv['invoice_number']} 付款日期 {txn['txn_date']}"
                    f" 晚于账期 {due_day}",
                    evidence,
                    pattern="late_payment",
                )
            if pay_day < parse_day(inv["invoice_date"]):
                self._add_diff(
                    DATE, "payment_prepaid",
                    f"发票 {inv['invoice_number']} 付款日期 {txn['txn_date']}"
                    f" 早于开票日（预付/合并结算可能）",
                    evidence,
                    pattern="prepayment",
                )
            if txn.get("payee_party_id") is None:
                self._add_diff(
                    PARTY, "payee_non_corporate",
                    f"发票 {inv['invoice_number']} 款项付至个人账户 "
                    f"{txn['payee_name']}，与销方主体不一致",
                    evidence,
                    detail={"payee_name": txn["payee_name"]},
                )
            elif txn["payee_party_id"] != inv["seller"]["party_id"]:
                self._add_diff(
                    PARTY, "payee_mismatch",
                    f"发票 {inv['invoice_number']} 收款方 {txn['payee_name']}"
                    f" 与销方 {inv['seller']['name']} 主体不一致",
                    evidence,
                )
            if txn["payer_name"] != inv["buyer"]["name"]:
                self._add_diff(
                    PARTY, "payer_mismatch",
                    f"付款账户名 {txn['payer_name']} 与购方 "
                    f"{inv['buyer']['name']} 不一致",
                    evidence,
                )

        if paid < payable - AMOUNT_TOLERANCE:
            self._add_diff(
                QUANTITY, "payment_short",
                f"发票 {inv['invoice_number']} 应付（扣退货折让）{payable}，"
                f"已付 {paid}，差额 {payable - paid}",
                evidence,
                detail={"payable": str(payable), "paid": str(paid)},
            )
        elif paid > payable + AMOUNT_TOLERANCE:
            self._add_diff(
                QUANTITY, "payment_excess",
                f"发票 {inv['invoice_number']} 应付 {payable}，"
                f"已付 {paid}，多付 {paid - payable}（可能为合并付款分摊差异）",
                evidence,
                pattern="merged_payment",
            )

    # ------------------------------------------------------------- 其余输出

    def _collect_unmatched(self, ledgers, counts, payments, invoices,
                           claimed_ledger, claimed_txn) -> dict:
        invoice_numbers = {e["content"]["invoice_number"] for e in invoices}
        invoice_batches = set()
        for e in invoices:
            invoice_batches.update(_delivery_batches(e["content"]).keys())

        orphan_ledger, orphan_payments, orphan_stock = [], [], []
        for env in ledgers:
            for entry in env["content"]["entries"]:
                if entry["direction"] == "purchase_in" and entry["entry_id"] not in claimed_ledger:
                    if not set(entry.get("ref_numbers", [])) & invoice_numbers:
                        orphan_ledger.append({
                            "entry_id": entry["entry_id"],
                            "store_id": env["content"]["store_id"],
                            "product_code": entry["product_code"],
                            "batch_no": entry["batch_no"],
                            "qty": entry["qty"],
                            "ref": self.registry.ref_of(env).as_dict(),
                        })
        for env in payments:
            for txn in env["content"]["transactions"]:
                unknown = [a["invoice_number"] for a in txn["allocations"]
                           if a["invoice_number"] not in invoice_numbers]
                if unknown:
                    orphan_payments.append({
                        "txn_id": txn["txn_id"],
                        "unknown_invoice_numbers": unknown,
                        "amount": txn["amount"],
                        "ref": self.registry.ref_of(env).as_dict(),
                    })
        for env in counts:
            for item in env["content"]["items"]:
                if (item["product_code"], item["batch_no"]) not in invoice_batches \
                        and _d(item["total_qty"]) > 0:
                    orphan_stock.append({
                        "store_id": env["content"]["store_id"],
                        "product_code": item["product_code"],
                        "batch_no": item["batch_no"],
                        "total_qty": item["total_qty"],
                        "ref": self.registry.ref_of(env).as_dict(),
                    })
        return {"ledger_entries": orphan_ledger,
                "payments": orphan_payments,
                "stock_items": orphan_stock}

    def _coverage_warnings(self, invoices, ledgers, counts) -> list[dict]:
        warnings = []
        ledger_stores = {(e["content"]["pharmacy_id"], e["content"]["store_id"]) for e in ledgers}
        count_stores = {(e["content"]["pharmacy_id"], e["content"]["store_id"]) for e in counts}
        for env in invoices:
            inv = env["content"]
            key = (inv["pharmacy_id"], inv["store_id"])
            if key not in ledger_stores:
                warnings.append({
                    "kind": "ledger_coverage_gap",
                    "store_id": inv["store_id"],
                    "invoice_number": inv["invoice_number"],
                    "message": f"门店 {inv['store_id']} 未提交进销存片段，账方向无法核验",
                })
            if key not in count_stores:
                warnings.append({
                    "kind": "stock_coverage_gap",
                    "store_id": inv["store_id"],
                    "invoice_number": inv["invoice_number"],
                    "message": f"门店 {inv['store_id']} 无现场清点，实方向无法核验",
                })
        return warnings


def run_reconciliation(registry: SubmissionRegistry, case_id: str, as_of: str) -> dict:
    return Reconciliation(registry, case_id, as_of).run()
