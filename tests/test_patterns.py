"""对最小合成案例的口径锁定：常见商业情形标注与箱盒换算。

fixtures/case_2026_03_001.json 之外，这里用最小信封构造专门场景，
断言哪些差异必须挂 explainable_pattern（open），哪些不能挂。
"""

import unittest

from src import SubmissionRegistry, run_reconciliation

CASE = "case-synth"
SELLER = {"party_id": "sup-x", "name": "虚构供应商X", "tax_id": "91FAKEX"}
BUYER = {"party_id": "ph-y", "name": "虚构药店Y", "tax_id": "91FAKEY"}


def make_invoice(number, date, qty, *, notes=None, returns=None,
                 total=None, terms=30, seal_copy=False):
    notes = notes if notes is not None else [(f"DN-{number}", date, qty, "B1")]
    return {
        "submission_id": f"sub-{number}", "case_id": CASE,
        "material_type": "invoice_pack", "material_id": number, "version": 1,
        "submitted_by": {"party_id": "sup-x", "role": "supplier"},
        "submitted_at": f"{date}T18:00:00",
        "content": {
            "invoice_id": number, "invoice_number": number, "invoice_code": "VAT",
            "template_id": "TPL-X", "invoice_date": date,
            "seller": SELLER, "buyer": BUYER,
            "pharmacy_id": "ph-y", "store_id": "ST-Y",
            "lines": [{"line_no": 1, "product_code": "P1", "product_name": "虚构药",
                       "unit": "盒", "qty": qty, "unit_price": 10,
                       "amount": total if total is not None else qty * 10}],
            "total_amount": total if total is not None else qty * 10,
            "has_copy_seal": seal_copy, "has_original_seal": True,
            "payment_terms_days": terms,
            "delivery_notes": [
                {"note_id": f"dn-{number}-{i}", "note_number": nno,
                 "note_date": ndate,
                 "lines": [{"line_no": 1, "product_code": "P1",
                            "batch_no": batch, "qty": nqty}]}
                for i, (nno, ndate, nqty, batch) in enumerate(notes)],
            "return_documents": [
                {"doc_id": f"rt-{number}", "doc_number": f"RT-{number}",
                 "doc_date": rdate, "amount": ramount,
                 "lines": [{"product_code": "P1", "batch_no": "B1",
                            "qty": rqty}]}
                for rdate, rqty, ramount in (returns or [])],
        },
    }


def make_ledger(entries, *, period_to="2026-06-30"):
    return {
        "submission_id": "sub-ledger", "case_id": CASE,
        "material_type": "ledger_snippet", "material_id": "ledger-y",
        "version": 1,
        "submitted_by": {"party_id": "ph-y", "role": "pharmacy"},
        "submitted_at": "2026-03-01T09:00:00",
        "content": {
            "snippet_id": "ledger-y", "pharmacy_id": "ph-y",
            "store_id": "ST-Y", "period_from": "2026-01-01",
            "period_to": period_to, "entries": entries,
        },
    }


def purchase_entry(eid, date, number, qty):
    return {"entry_id": eid, "entry_date": date, "direction": "purchase_in",
            "ref_numbers": [number], "supplier_id": "sup-x",
            "product_code": "P1", "batch_no": "B1", "qty": qty}


def return_entry(eid, date, doc_number, qty):
    return {"entry_id": eid, "entry_date": date, "direction": "return_out",
            "ref_numbers": [doc_number], "supplier_id": "sup-x",
            "product_code": "P1", "batch_no": "B1", "qty": qty}


def make_count(date, total, *, box_spec=10, box_count=None, loose=0):
    if box_count is None:
        box_count, loose = divmod(total, box_spec)
    return {
        "submission_id": "sub-count", "case_id": CASE,
        "material_type": "stock_count", "material_id": "count-y", "version": 1,
        "submitted_by": {"party_id": "insp", "role": "inspector"},
        "submitted_at": f"{date}T16:00:00",
        "content": {
            "count_id": "count-y", "pharmacy_id": "ph-y", "store_id": "ST-Y",
            "counted_at": date, "inspector_name": "虚构检查员",
            "items": [{"product_code": "P1", "batch_no": "B1",
                       "box_spec": box_spec, "box_count": box_count,
                       "loose_qty": loose, "total_qty": total}],
        },
    }


def make_payment(txn_id, date, number, amount):
    return {
        "submission_id": "sub-pay", "case_id": CASE,
        "material_type": "payment_summary", "material_id": "pay-y", "version": 1,
        "submitted_by": {"party_id": "ph-y", "role": "pharmacy"},
        "submitted_at": "2026-03-01T09:00:00",
        "content": {
            "summary_id": "pay-y", "pharmacy_id": "ph-y",
            "account_holder_name": "虚构药店Y",
            "transactions": [{
                "txn_id": txn_id, "txn_date": date,
                "payer_name": "虚构药店Y", "payee_name": "虚构供应商X",
                "payee_party_id": "sup-x", "amount": amount,
                "allocations": [{"invoice_number": number, "amount": amount}],
            }],
        },
    }


def build(envelopes, as_of="2026-03-20"):
    reg = SubmissionRegistry()
    for env in envelopes:
        reg.submit(env)
    return run_reconciliation(reg, CASE, as_of)


def only_candidate(result):
    return result["candidates"][0]


class PartialDeliveryTest(unittest.TestCase):
    def test_undelivered_portion_is_benign_open(self):
        # 开票 100，首单只到 60；台账按实到 60 入账，盘点 60；账期内未付款
        result = build([
            make_invoice("INV-P1", "2026-03-01", 100,
                         notes=[("DN-P1", "2026-03-01", 60, "B1")]),
            make_ledger([purchase_entry("e1", "2026-03-01", "INV-P1", 60)]),
            make_count("2026-03-10", 60),
        ], as_of="2026-03-10")
        cand = only_candidate(result)
        self.assertEqual(cand["status"], "open")
        patterns = {d["explainable_pattern"] for d in cand["differences"]}
        self.assertIn("partial_delivery", patterns)
        self.assertIn("credit_sale", patterns)

    def test_split_deliveries_completing_quantity_reconcile(self):
        # 60+40 分两日送齐，无任何差异
        result = build([
            make_invoice("INV-P2", "2026-03-01", 100, notes=[
                ("DN-P2A", "2026-03-01", 60, "B1"),
                ("DN-P2B", "2026-03-02", 40, "B1")]),
            make_ledger([
                purchase_entry("e1", "2026-03-01", "INV-P2", 60),
                purchase_entry("e2", "2026-03-02", "INV-P2", 40)]),
            make_count("2026-03-10", 100),
            make_payment("t1", "2026-03-05", "INV-P2", 1000),
        ])
        self.assertEqual(only_candidate(result)["status"], "reconciled")
        self.assertEqual(only_candidate(result)["differences"], [])


class ReturnPatternTest(unittest.TestCase):
    def test_return_without_ledger_entry_is_benign_open(self):
        # 红字退货凭证已到但台账尚未作购退出账；实物仍为 10，付款按退货后 80
        result = build([
            make_invoice("INV-R1", "2026-03-01", 10,
                         returns=[("2026-03-03", 2, 20)]),
            make_ledger([purchase_entry("e1", "2026-03-01", "INV-R1", 10)]),
            make_count("2026-03-10", 10),
            make_payment("t1", "2026-03-06", "INV-R1", 80),
        ])
        cand = only_candidate(result)
        self.assertEqual(cand["status"], "open")
        self.assertEqual(
            [d["kind"] for d in cand["differences"]], ["ledger_return_missing"])
        self.assertEqual(cand["differences"][0]["explainable_pattern"], "return")

    def test_return_fully_booked_reconciles(self):
        result = build([
            make_invoice("INV-R2", "2026-03-01", 10,
                         returns=[("2026-03-03", 2, 20)]),
            make_ledger([
                purchase_entry("e1", "2026-03-01", "INV-R2", 10),
                return_entry("e2", "2026-03-03", "RT-INV-R2", 2)]),
            make_count("2026-03-10", 8),
            make_payment("t1", "2026-03-06", "INV-R2", 80),
        ])
        self.assertEqual(only_candidate(result)["status"], "reconciled")


class LatePaymentTest(unittest.TestCase):
    def test_payment_after_terms_is_marked_benign(self):
        result = build([
            make_invoice("INV-L1", "2026-01-01", 10, terms=30),
            make_ledger([purchase_entry("e1", "2026-01-01", "INV-L1", 10)]),
            make_count("2026-01-10", 10),
            make_payment("t1", "2026-02-15", "INV-L1", 100),
        ], as_of="2026-03-20")
        cand = only_candidate(result)
        self.assertEqual(cand["status"], "open")
        self.assertEqual(cand["differences"][0]["explainable_pattern"],
                         "late_payment")


class BoxArithmeticTest(unittest.TestCase):
    def test_box_total_inconsistency_is_non_benign_review(self):
        # 1 盒规 10 应为 10，total_qty 却报 9；同时库存差
        result = build([
            make_invoice("INV-B1", "2026-03-01", 10),
            make_ledger([purchase_entry("e1", "2026-03-01", "INV-B1", 10)]),
            make_count("2026-03-10", 9, box_spec=10, box_count=1, loose=0),
            make_payment("t1", "2026-03-05", "INV-B1", 100),
        ])
        cand = only_candidate(result)
        kinds = {d["kind"]: d for d in cand["differences"]}
        self.assertIn("box_count_mismatch", kinds)
        self.assertIn("stock_variance", kinds)
        self.assertIsNone(kinds["box_count_mismatch"]["explainable_pattern"])
        self.assertEqual(cand["status"], "review")


class CopySealTest(unittest.TestCase):
    def test_copy_seal_alone_is_neither_diff_nor_status(self):
        result = build([
            make_invoice("INV-S1", "2026-03-01", 10, seal_copy=True),
            make_ledger([purchase_entry("e1", "2026-03-01", "INV-S1", 10)]),
            make_count("2026-03-10", 10),
            make_payment("t1", "2026-03-05", "INV-S1", 100),
        ])
        cand = only_candidate(result)
        self.assertEqual(cand["status"], "reconciled")
        self.assertTrue(cand["seal_facts"]["has_copy_seal"])


if __name__ == "__main__":
    unittest.main()
