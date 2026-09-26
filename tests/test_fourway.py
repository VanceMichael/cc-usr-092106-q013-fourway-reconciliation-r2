"""票账货款四向核验端到端与单元测试（全部使用虚构样例）。"""

import copy
import json
import unittest
from pathlib import Path

from src import Actor, Casework, SubmissionRegistry, run_reconciliation
from src.contracts import ContractError
from src.matcher import MISSING, QUANTITY, DATE, PARTY
from src.registry import RegistryError

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "case_2026_03_001.json"
CATEGORIES = {MISSING, QUANTITY, DATE, PARTY}


def load_case():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def build_registry(case):
    reg = SubmissionRegistry()
    for env in case["submissions"]:
        reg.submit(copy.deepcopy(env))
    return reg


def run_case(reg=None, case=None):
    case = case or load_case()
    reg = reg or build_registry(case)
    return reg, run_reconciliation(reg, case["case_id"], case["as_of"])


def candidate(result, invoice_number):
    return next(c for c in result["candidates"]
                if c["invoice_number"] == invoice_number)


def kinds(result, invoice_number):
    return [(d["category"], d["kind"], d["explainable_pattern"])
            for d in candidate(result, invoice_number)["differences"]]


class FixtureContractTest(unittest.TestCase):
    def test_every_submission_passes_contract(self):
        case = load_case()
        reg = SubmissionRegistry()
        for env in case["submissions"]:
            reg.submit(copy.deepcopy(env))
        self.assertEqual(len(case["submissions"]), 15)

    def test_unknown_field_rejected(self):
        case = load_case()
        env = copy.deepcopy(case["submissions"][0])
        env["submission_id"] = "sub-bad"
        env["content"]["evil"] = 1
        with self.assertRaises(ContractError):
            SubmissionRegistry().submit(env)

    def test_bad_material_type_rejected(self):
        case = load_case()
        env = copy.deepcopy(case["submissions"][0])
        env["material_type"] = "wechat_chatlog"
        with self.assertRaises(ContractError):
            SubmissionRegistry().submit(env)


class RegistryAppendOnlyTest(unittest.TestCase):
    def test_duplicate_submission_id_rejected(self):
        case = load_case()
        reg = build_registry(case)
        with self.assertRaises(RegistryError):
            reg.submit(copy.deepcopy(case["submissions"][0]))

    def test_version_must_increase_and_history_retained(self):
        case = load_case()
        reg = build_registry(case)
        versions = reg.versions("ledger_snippet", "ledger-st001")
        self.assertEqual([e["version"] for e in versions], [1, 2])
        self.assertEqual(reg.latest("ledger_snippet", "ledger-st001")["version"], 2)

        old = copy.deepcopy(versions[0])
        old["submission_id"] = "sub-ledger-st001-v3"
        old["version"] = 1
        with self.assertRaises(RegistryError):
            reg.submit(old)

    def test_engine_reads_latest_version_only(self):
        # v1 片段中 INV-0310 仅入账 50（数量差），v2 修正为 60；
        # 以完整登记构建时 INV-0310 不出现数量差异。
        _, result = run_case()
        for category, kind, _ in kinds(result, "INV-0310"):
            self.assertNotEqual(kind, "ledger_inbound_short")


class MatcherHappyPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.result = run_case()

    def test_three_clean_invoices_reconciled(self):
        for number in ("INV-0301", "INV-0302", "INV-0305"):
            self.assertEqual(candidate(self.result, number)["status"], "reconciled",
                             candidate(self.result, number)["differences"])

    def test_split_delivery_is_not_a_discrepancy(self):
        # INV-0302：120+80 两张同行单分两日送齐 200
        self.assertEqual(kinds(self.result, "INV-0302"), [])

    def test_discount_and_return_compute_payable(self):
        # 折让 100 已在行内；退货 200 冲减应付：1000-100(行折让已含总价900)-200=700
        self.assertEqual(candidate(self.result, "INV-0305")["payable_amount"], "700")
        self.assertEqual(kinds(self.result, "INV-0305"), [])

    def test_returned_goods_stock_reconciles(self):
        # 100 购入 - 20 退货 - 10 清点前销售 = 70，与清点一致；
        # 清点日后（03-16）的销售不得影响应有库存。
        stock_diffs = [d for c in self.result["candidates"] for d in c["differences"]
                       if d["kind"] == "stock_variance"]
        batches = {(d["detail"]["product_code"], d["detail"]["batch_no"])
                   for d in stock_diffs}
        self.assertNotIn(("P-AMX", "B2603"), batches)

    def test_merged_payment_allocates_exactly(self):
        # txn-9001 一笔 1700 合并支付 INV-0301 与 INV-0305
        for number in ("INV-0301", "INV-0305"):
            joined = " ".join(k for _, k, _ in kinds(self.result, number))
            self.assertNotIn("payment_short", joined)
            self.assertNotIn("payment_excess", joined)


class MatcherBenignPatternTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.result = run_case()

    def test_credit_sale_within_terms_is_open_not_violation(self):
        for number in ("INV-0310", "INV-0313"):
            cand = candidate(self.result, number)
            self.assertEqual(cand["status"], "open")
            patterns = {p for _, _, p in kinds(self.result, number) if p}
            self.assertEqual(patterns, {"credit_sale"})

    def test_engine_never_emits_a_violation_verdict(self):
        text = json.dumps(self.result, ensure_ascii=False)
        self.assertNotIn("violation", text)

    def test_all_differences_use_only_four_categories(self):
        for cand in self.result["candidates"]:
            for d in cand["differences"]:
                self.assertIn(d["category"], CATEGORIES)

    def test_benign_patterns_are_individually_marked(self):
        marked = {d["explainable_pattern"]
                  for c in self.result["candidates"] for d in c["differences"]}
        # 本批样例至少覆盖赊销；折让/退货/分批/合并均在 reconciled 中静默通过
        self.assertIn("credit_sale", marked)


class MatcherRealDifferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.result = run_case()

    def test_stock_variance_with_box_count_detail(self):
        _, k, _ = [(c, k, p) for c, k, p in kinds(self.result, "INV-0315")
                   if k == "stock_variance"][0]
        self.assertEqual(k, "stock_variance")
        detail = next(d for d in candidate(self.result, "INV-0315")["differences"]
                      if d["kind"] == "stock_variance")["detail"]
        self.assertEqual(detail["expected"], "80")
        self.assertEqual(detail["actual"], "60")

    def test_payer_party_mismatch(self):
        self.assertIn((PARTY, "payer_mismatch", None), kinds(self.result, "INV-0315"))

    def test_payee_personal_account_is_party_diff(self):
        self.assertIn((PARTY, "payee_non_corporate", None),
                      kinds(self.result, "INV-0312"))

    def test_date_conflicts_separated(self):
        ks = kinds(self.result, "INV-0309")
        self.assertIn((DATE, "delivery_before_invoice", None), ks)
        self.assertIn((DATE, "ledger_date_out_of_window", None), ks)

    def test_missing_delivery_note_and_ledger_entry(self):
        ks = kinds(self.result, "INV-0312")
        self.assertIn((MISSING, "delivery_note_missing", None), ks)
        self.assertIn((MISSING, "ledger_inbound_missing", None), ks)

    def test_coverage_gaps_do_not_become_false_missing_items(self):
        warnings = {(w["kind"], w["invoice_number"])
                    for w in self.result["coverage_warnings"]}
        self.assertIn(("stock_coverage_gap", "INV-0312"), warnings)
        self.assertIn(("ledger_coverage_gap", "INV-0313"), warnings)
        # 无清点门店不应额外产生 stock_item_missing 差异
        for c in self.result["candidates"]:
            if c["invoice_number"] in {"INV-0312", "INV-0313"}:
                self.assertNotIn("stock_count_missing",
                                 [d["kind"] for d in c["differences"]])

    def test_orphan_materials_listed_separately(self):
        unmatched = self.result["unmatched"]
        self.assertEqual([x["entry_id"] for x in unmatched["ledger_entries"]],
                         ["e-201"])
        self.assertEqual(
            [(x["txn_id"], x["unknown_invoice_numbers"])
             for x in unmatched["payments"]],
            [("txn-9900", ["INV-8888"])])
        self.assertEqual(
            [(x["product_code"], x["batch_no"]) for x in unmatched["stock_items"]],
            [("P-OLD", "B2599")])


class SealIrrelevanceTest(unittest.TestCase):
    def test_toggling_seal_facts_does_not_change_findings(self):
        case = load_case()
        _, baseline = run_case(case=case)

        case2 = copy.deepcopy(case)
        for env in case2["submissions"]:
            if env["material_type"] == "invoice_pack":
                env["submission_id"] = env["submission_id"] + "-sealflip"
                env["content"]["has_copy_seal"] = not env["content"]["has_copy_seal"]
                env["content"]["has_original_seal"] = not env["content"]["has_original_seal"]
        reg2 = SubmissionRegistry()
        for env in case2["submissions"]:
            reg2.submit(env)
        flipped = run_reconciliation(reg2, case2["case_id"], case2["as_of"])

        def signature(result):
            return [(c["invoice_number"], c["status"], c["payable_amount"],
                     [(d["category"], d["kind"], d["explainable_pattern"])
                      for d in c["differences"]]) for c in result["candidates"]]

        self.assertEqual(signature(baseline), signature(flipped))


class DecisionAndTraceTest(unittest.TestCase):
    def setUp(self):
        self.reg, self.result = run_case()
        self.cw = Casework(self.reg)
        self.cand = candidate(self.result, "INV-0312")
        self.diff = next(d for d in self.cand["differences"]
                         if d["kind"] == "payee_non_corporate")
        self.inv_env = self.reg.latest("invoice_pack", "inv-0312")
        self.pay_env = self.reg.latest("payment_summary", "pay-01")
        self.decision = {
            "decision_id": "dec-001",
            "case_id": "case-2026-03-001",
            "diff_id": self.diff["diff_id"],
            "conclusion": "violation",
            "decided_by": "insp-01",
            "decided_at": "2026-03-21T10:00:00",
            "rationale": "货款付至个人账户且票货俱缺（虚构）",
            "evidence_snapshot": [
                {"submission_id": self.inv_env["submission_id"], "version": 1,
                 "material_type": "invoice_pack", "material_id": "inv-0312"},
                {"submission_id": self.pay_env["submission_id"], "version": 1,
                 "material_type": "payment_summary", "material_id": "pay-01"},
            ],
        }

    def test_only_inspector_may_decide(self):
        for actor in (Actor("sup-01", "supplier"), Actor("ph-01", "pharmacy")):
            with self.assertRaises(Exception):
                self.cw.record_decision(self.decision, actor)

    def test_decision_recorded_and_immutable(self):
        self.cw.record_decision(self.decision, Actor.inspector("insp-01"))
        with self.assertRaises(Exception):
            self.cw.record_decision(self.decision, Actor.inspector("insp-01"))
        self.assertEqual(
            self.cw.decisions_for_diff(self.diff["diff_id"])[0]["conclusion"],
            "violation")

    def test_snapshot_must_reference_existing_version(self):
        bad = copy.deepcopy(self.decision)
        bad["decision_id"] = "dec-bad"
        bad["evidence_snapshot"][0]["submission_id"] = "sub-does-not-exist"
        with self.assertRaises(Exception):
            self.cw.record_decision(bad, Actor.inspector("insp-01"))

    def test_trace_returns_four_way_evidence_and_remediation(self):
        self.cw.record_decision(self.decision, Actor.inspector("insp-01"))
        self.cw.add_remediation("dec-001", {
            "remediation_id": "rem-001",
            "description": "退回医保基金并整改对公结算（虚构）",
            "due_date": "2026-04-15", "status": "open",
        }, Actor.inspector("insp-01"))
        trace = self.cw.trace(self.result, self.diff["diff_id"])
        refs = {e["ref"]["material_type"] for e in trace["evidence"]}
        self.assertSetEqual(refs, {"invoice_pack", "payment_summary"})
        self.assertEqual(trace["decisions"][0]["remediations"][0]["status"], "open")

    def test_remediation_closing_appends_history(self):
        self.cw.record_decision(self.decision, Actor.inspector("insp-01"))
        self.cw.add_remediation("dec-001", {
            "remediation_id": "rem-001", "description": "整改",
            "due_date": "2026-04-15", "status": "open",
        }, Actor.inspector("insp-01"))
        self.cw.update_remediation("dec-001", "rem-001", "closed",
                                   "2026-04-10", Actor.inspector("insp-01"))
        rem = self.cw.remediations("dec-001")[0]
        self.assertEqual(rem["status"], "closed")
        self.assertEqual(rem["history"][0]["status"], "open")

    def test_new_version_flags_snapshot_stale_but_keeps_conclusion(self):
        self.cw.record_decision(self.decision, Actor.inspector("insp-01"))
        v2 = copy.deepcopy(self.inv_env)
        v2["submission_id"] = "sub-inv-0312-v2"
        v2["version"] = 2
        v2["supersedes"] = self.inv_env["submission_id"]
        v2["submitted_at"] = "2026-03-22T09:00:00"
        self.reg.submit(v2)

        trace = self.cw.trace(self.result, self.diff["diff_id"])
        self.assertTrue(trace["decisions"][0]["snapshot_stale"])
        self.assertEqual(trace["decisions"][0]["conclusion"], "violation")
        inv_evidence = next(e for e in trace["evidence"]
                            if e["ref"]["material_type"] == "invoice_pack")
        self.assertTrue(inv_evidence["has_newer_version"])
        self.assertEqual(len(inv_evidence["version_history"]), 2)


class ExplanationVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg, cls.result = run_case()
        cls.cw = Casework(cls.reg)
        cls.case_id = "case-2026-03-001"

    def test_visibility_matrix(self):
        def ids(actor):
            return {e["content"]["explanation_id"]
                    for e in self.cw.visible_explanations(actor, self.case_id)}

        # ex-01：供应商 sup-01 的 internal 说明，仅提交方与检查方可见
        self.assertEqual(ids(Actor("sup-01", "supplier")), {"ex-01"})
        self.assertEqual(ids(Actor("ph-01", "pharmacy")), {"ex-02"})
        # ex-02：药店共享给采购相对方；INV-0310 销方为 sup-02
        self.assertEqual(ids(Actor("sup-02", "supplier")), {"ex-02"})
        # 无关第三方供应商什么都看不到
        self.assertEqual(ids(Actor("sup-99", "supplier")), set())
        self.assertEqual(
            ids(Actor.inspector("insp-01")), {"ex-01", "ex-02"})


class TemplateClusterTest(unittest.TestCase):
    def test_same_template_recurring_across_stores(self):
        reg, _ = run_case()
        cw = Casework(reg)
        clusters = cw.template_clusters()
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster["template_id"], "TPL-88")
        self.assertEqual(cluster["store_count"], 3)
        self.assertEqual(cluster["seller_count"], 1)
        self.assertTrue(cluster["recurring_pattern"])
        self.assertEqual(
            [i["invoice_number"] for i in cluster["invoices"]],
            ["INV-0302", "INV-0305", "INV-0312", "INV-0313"])
        self.assertIn("ST-003", cluster["stores"])


if __name__ == "__main__":
    unittest.main()
