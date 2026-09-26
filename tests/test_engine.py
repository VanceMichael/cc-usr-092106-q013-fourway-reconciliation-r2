import unittest

from _helpers import AS_OF, load_case
from src.engine import DifferenceCategory, reconcile


def summaries(report, category):
    return [d.summary for d in report.differences if d.category == category]


class NormalCaseTest(unittest.TestCase):
    """五种正常业务情形（分批送货、赊销、退货、折让、合并付款、票据统计
    更正）不得产生任何线索。"""

    @classmethod
    def setUpClass(cls):
        bundle, store = load_case("case_normal.json")
        cls.report = reconcile(bundle, store, AS_OF)

    def test_零差异(self):
        self.assertEqual(self.report.differences, [])

    def test_候选按采购关系提出(self):
        self.assertEqual(len(self.report.candidates), 5)
        self.assertTrue(all(c.relation_id == "REL-1" for c in self.report.candidates))

    def test_分批送货合并计入候选(self):
        cand = next(c for c in self.report.candidates if c.invoice_key.startswith("INV-1@"))
        self.assertEqual(cand.delivery_note_nos, ("DN-1A", "DN-1B"))
        self.assertEqual(cand.ledger_entry_ids, ("LE-1A", "LE-1B"))

    def test_合并付款计入两张票的候选(self):
        pay = {
            c.invoice_key.split("@")[0]: c.payment_ids for c in self.report.candidates
        }
        self.assertEqual(pay["INV-4"], ("PAY-45",))
        self.assertEqual(pay["INV-5"], ("PAY-45",))

    def test_更正后按最新版本匹配(self):
        keys = {c.invoice_key for c in self.report.candidates}
        self.assertIn("INV-5@v2", keys)
        self.assertNotIn("INV-5@v1", keys)

    def test_同一供应商模板重复使用不构成串用(self):
        self.assertEqual(self.report.template_clusters, [])


class AnomalyCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bundle, store = load_case("case_anomalies.json")
        cls.report = reconcile(bundle, store, AS_OF)
        cls.grouped = cls.report.by_category()

    def test_四类差异分开呈现(self):
        self.assertEqual(set(self.grouped), set(DifferenceCategory))
        for cat in DifferenceCategory:
            self.assertGreater(len(self.grouped[cat]), 0, cat)

    def test_缺项(self):
        text = "\n".join(summaries(self.report, DifferenceCategory.MISSING))
        self.assertIn("INV-MISS 缺随货同行单", text)
        self.assertIn("INV-MISS 未见付款", text)
        self.assertIn("PAY-ORPHAN 未注明对应发票", text)

    def test_数量差(self):
        text = "\n".join(summaries(self.report, DifferenceCategory.QUANTITY))
        self.assertIn("票面 100，同行单累计交付 90", text)   # 票↔货
        self.assertIn("交付 90，台账入库 70", text)          # 货↔账
        self.assertIn("账面 80，实盘 66", text)              # 账↔实
        self.assertIn("付款合计 4500 分与应付 5000 分", text)  # 票↔款

    def test_日期冲突(self):
        text = "\n".join(summaries(self.report, DifferenceCategory.DATE))
        self.assertIn("不在采购关系 REL-1 有效期内", text)
        self.assertIn("早于发票 INV-DATE 开票日", text)   # 发货日、付款日各一条
        self.assertIn("早于最早发货日", text)              # 台账入库日

    def test_主体不一致(self):
        text = "\n".join(summaries(self.report, DifferenceCategory.PARTY))
        self.assertIn("SUP-2→PHA-1 无登记采购关系", text)
        self.assertIn("主体与发票 INV-PARTY 不一致", text)  # 同行单主体
        self.assertIn("与发票 INV-PARTY 主体不符", text)    # 付款主体

    def test_模板跨门店串用(self):
        self.assertEqual(len(self.report.template_clusters), 1)
        cluster = self.report.template_clusters[0]
        self.assertEqual(cluster.template_id, "TPL-COMMON")
        self.assertEqual(cluster.pharmacy_ids, ("PHA-1", "PHA-2"))
        self.assertEqual(cluster.supplier_ids, ("SUP-1", "SUP-2"))

    def test_差异都带原始依据引用(self):
        for diff in self.report.differences:
            self.assertTrue(diff.refs, diff.difference_id)

    def test_引擎不输出违规结论(self):
        # 差异对象只有类别、摘要与引用，没有“违规/处罚”字段
        for diff in self.report.differences:
            self.assertFalse(hasattr(diff, "violation"))
            self.assertFalse(hasattr(diff, "verdict"))


if __name__ == "__main__":
    unittest.main()
