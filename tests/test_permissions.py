import unittest
from datetime import date

from src.models import Explanation
from src.permissions import (
    Role,
    Viewer,
    can_submit_explanation,
    filter_explanations,
)

SUP_EXPLANATION = Explanation(
    explanation_id="E-1",
    difference_id="D0001",
    author_party_id="SUP-1",
    text="该批为赊销，账期内付款。",
    submitted_at=date(2026, 9, 1),
)
PHA_EXPLANATION = Explanation(
    explanation_id="E-2",
    difference_id="D0001",
    author_party_id="PHA-1",
    text="退货已在 7 月登记。",
    submitted_at=date(2026, 9, 2),
)


class PermissionTest(unittest.TestCase):
    def test_检查员可看双方说明(self):
        viewer = Viewer(Role.INSPECTOR)
        rows = filter_explanations([SUP_EXPLANATION, PHA_EXPLANATION], viewer)
        self.assertEqual(len(rows), 2)

    def test_负责人可看双方说明(self):
        viewer = Viewer(Role.LEAD)
        rows = filter_explanations([SUP_EXPLANATION, PHA_EXPLANATION], viewer)
        self.assertEqual(len(rows), 2)

    def test_供应商只见本方说明(self):
        viewer = Viewer(Role.SUPPLIER, party_id="SUP-1")
        rows = filter_explanations([SUP_EXPLANATION, PHA_EXPLANATION], viewer)
        self.assertEqual([e.explanation_id for e in rows], ["E-1"])

    def test_药店只见本方说明(self):
        viewer = Viewer(Role.PHARMACY, party_id="PHA-1")
        rows = filter_explanations([SUP_EXPLANATION, PHA_EXPLANATION], viewer)
        self.assertEqual([e.explanation_id for e in rows], ["E-2"])

    def test_外部账号只能以自己名义提交(self):
        self.assertTrue(
            can_submit_explanation(Viewer(Role.SUPPLIER, party_id="SUP-1"), "SUP-1")
        )
        self.assertFalse(
            can_submit_explanation(Viewer(Role.SUPPLIER, party_id="SUP-1"), "PHA-1")
        )
        self.assertFalse(can_submit_explanation(Viewer(Role.INSPECTOR), "SUP-1"))


if __name__ == "__main__":
    unittest.main()
