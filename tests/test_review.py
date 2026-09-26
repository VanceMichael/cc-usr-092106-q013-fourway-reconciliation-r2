import unittest
from datetime import date

from _helpers import AS_OF, load_case
from src.engine import DifferenceCategory, reconcile
from src.models import EvidenceKind, Explanation
from src.permissions import Role, Viewer, can_submit_explanation
from src.review import (
    DecisionKind,
    InspectionCase,
    RectificationStatus,
    build_trace,
    close_case,
    complete_rectification,
    evidence_backing,
    open_rectification,
    record_decision,
)

INSPECTOR = Viewer(Role.INSPECTOR, party_id="U-检查员甲")
LEAD = Viewer(Role.LEAD, party_id="U-负责人乙")
SUPPLIER = Viewer(Role.SUPPLIER, party_id="SUP-1")


class ReviewTraceTest(unittest.TestCase):
    def setUp(self):
        self.bundle, self.store = load_case("case_anomalies.json")
        self.report = reconcile(self.bundle, self.store, AS_OF)
        self.case = InspectionCase(
            case_id="CASE-1", title="虚构专项检查", opened_at=AS_OF
        )

    def _difference(self, text_fragment):
        for diff in self.report.differences:
            if text_fragment in diff.summary:
                return diff
        self.fail(f"未找到包含 {text_fragment} 的差异")

    def test_证据回到四类原始依据(self):
        diff = self._difference("票面 100")
        backing = evidence_backing(diff)
        self.assertTrue(backing.invoice)
        self.assertTrue(backing.delivery_note)
        self.assertEqual(backing.payment, ())  # 该差异与款无关

    def test_供应商不能下结论(self):
        diff = self._difference("票面 100")
        with self.assertRaises(ValueError):
            record_decision(
                self.case, diff, DecisionKind.CONFIRMED, "系统不得自动定性",
                SUPPLIER, AS_OF,
            )

    def test_完整溯源链(self):
        diff = self._difference("票面 100")

        # 双方各自提交说明
        sup_note = Explanation(
            explanation_id="E-S", difference_id=diff.difference_id,
            author_party_id="SUP-1", text="10 盒为分批尾货，在途中。",
            submitted_at=AS_OF,
        )
        pha_note = Explanation(
            explanation_id="E-P", difference_id=diff.difference_id,
            author_party_id="PHA-1", text="台账按实收登记。",
            submitted_at=AS_OF,
        )
        self.assertTrue(can_submit_explanation(SUPPLIER, "SUP-1"))
        self.store.add_explanation(sup_note)
        self.store.add_explanation(pha_note)

        decision = record_decision(
            self.case, diff, DecisionKind.NEEDS_FOLLOWUP,
            "尾货待补送，限下周提供第二张同行单", INSPECTOR, AS_OF,
        )
        rect = open_rectification(
            self.case, diff, "补交同行单并说明数量差", date(2026, 10, 10),
            INSPECTOR, AS_OF,
        )
        complete_rectification(
            self.case, rect.rectification_id, "已补交同行单 DN-Q2，差异消除",
            INSPECTOR, date(2026, 10, 5),
        )

        # 检查员视角：差异→四类依据→双方说明→人工决定→整改
        trace = build_trace(
            diff, self.case, self.store, INSPECTOR,
            self.report.candidates, self.report.template_clusters,
        )
        self.assertEqual(len(trace.explanations), 2)
        self.assertEqual(trace.decisions[0].decision_id, decision.decision_id)
        self.assertEqual(trace.rectifications[0].status, RectificationStatus.COMPLETED)
        self.assertEqual(trace.candidate.invoice_key, "INV-QTY@v1")
        self.assertIsNone(trace.template_cluster)

        # 决定快照冻结了当时的证据版本
        self.assertIn(EvidenceKind.INVOICE, {r.kind for r in decision.evidence_snapshot})
        snap_versions = {
            r.ref_id: r.version for r in decision.evidence_snapshot
            if r.kind == EvidenceKind.INVOICE
        }
        self.assertEqual(snap_versions.get("INV-QTY"), 1)

        # 供应商视角：看不到药店说明
        trace_sup = build_trace(
            diff, self.case, self.store, SUPPLIER,
            self.report.candidates, self.report.template_clusters,
        )
        self.assertEqual([e.explanation_id for e in trace_sup.explanations], ["E-S"])

    def test_模板串用聚类按差异票据关联(self):
        # 夹具中 TPL-COMMON 的两张票本身四向一致，不出差异；
        # 与其无关的差异溯源时不附带聚类，避免张冠李戴。
        diff = self._difference("票面 100")
        trace = build_trace(
            diff, self.case, self.store, INSPECTOR,
            self.report.candidates, self.report.template_clusters,
        )
        self.assertIsNone(trace.template_cluster)

        # 聚类本身可独立查看，覆盖两个不同门店
        cluster = self.report.template_clusters[0]
        self.assertEqual(cluster.pharmacy_ids, ("PHA-1", "PHA-2"))

    def test_只有负责人能关闭案卷(self):
        diff = self._difference("票面 100")
        with self.assertRaises(ValueError):
            close_case(self.case, INSPECTOR, AS_OF)
        close_case(self.case, LEAD, AS_OF)
        self.assertTrue(self.case.is_closed)
        with self.assertRaises(ValueError):
            record_decision(
                self.case, diff, DecisionKind.DISMISSED, "结案后不得追加结论",
                INSPECTOR, AS_OF,
            )

    def test_整改只能完成一次(self):
        diff = self._difference("票面 100")
        rect = open_rectification(
            self.case, diff, "补材料", date(2026, 10, 10), INSPECTOR, AS_OF
        )
        complete_rectification(self.case, rect.rectification_id, "ok", INSPECTOR, AS_OF)
        with self.assertRaises(ValueError):
            complete_rectification(
                self.case, rect.rectification_id, "再次完成", INSPECTOR, AS_OF
            )


if __name__ == "__main__":
    unittest.main()
