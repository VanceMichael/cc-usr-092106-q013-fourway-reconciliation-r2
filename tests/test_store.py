import unittest
from datetime import date

from _helpers import AS_OF, load_case
from src.models import Invoice, InvoiceLine
from src.quantities import Qty
from src.store import EvidenceStore, VersionConflict


def _invoice(invoice_id: str, version: int, amount: int = 1000) -> Invoice:
    return Invoice(
        invoice_id=invoice_id,
        version=version,
        supplier_id="SUP-1",
        pharmacy_id="PHA-1",
        issue_date=date(2026, 3, 1),
        lines=(
            InvoiceLine(product_id="P-100", qty=Qty(2, "盒"), unit_price_fen=amount // 2),
        ),
        template_id="TPL",
    )


class VersionedStoreTest(unittest.TestCase):
    def test_同版本不能覆盖(self):
        store = EvidenceStore()
        store.add_invoice(_invoice("INV", 1))
        with self.assertRaises(VersionConflict):
            store.add_invoice(_invoice("INV", 1, amount=2000))

    def test_版本号必须递增(self):
        store = EvidenceStore()
        store.add_invoice(_invoice("INV", 2))
        with self.assertRaises(VersionConflict):
            store.add_invoice(_invoice("INV", 1))

    def test_追加更正版本后旧版仍可查(self):
        store = EvidenceStore()
        store.add_invoice(_invoice("INV", 1))
        store.add_invoice(_invoice("INV", 2))
        versions = store.invoice_versions("INV")
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertEqual(store.latest_invoice("INV").version, 2)

    def test_夹具中更正版本被保留(self):
        _, store = load_case("case_normal.json")
        versions = store.invoice_versions("INV-5")
        self.assertEqual([v.version for v in versions], [1, 2])
        latest = store.latest_invoice("INV-5")
        self.assertEqual(latest.lines[0].qty.value, 20)
        self.assertEqual(latest.supersedes, "INV-5@v1")


if __name__ == "__main__":
    unittest.main()
