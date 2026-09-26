"""把 JSON 资料解析为领域对象。

资料分为主数据（主体、采购关系、商品单位）和四类证据。所有示例均为
虚构数据；解析器只做结构化与单位换算，不做任何真伪判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import (
    Allowance,
    DeliveryNote,
    Invoice,
    InvoiceLine,
    LedgerEntry,
    Party,
    PartyRole,
    Payment,
    Relation,
    ReturnNote,
    StockCount,
)
from .quantities import parse_qty


@dataclass
class EvidenceBundle:
    parties: dict[str, Party] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    product_units: dict[str, str] = field(default_factory=dict)
    product_per_box: dict[str, int] = field(default_factory=dict)
    invoices: list[Invoice] = field(default_factory=list)
    delivery_notes: list[DeliveryNote] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    stock_counts: list[StockCount] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    returns: list[ReturnNote] = field(default_factory=list)
    allowances: list[Allowance] = field(default_factory=list)

    def party(self, party_id: str) -> Party | None:
        return self.parties.get(party_id)


def _day(value: str) -> date:
    return date.fromisoformat(value)


def load_bundle(path: str | Path) -> EvidenceBundle:
    return parse_bundle(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_bundle(data: dict) -> EvidenceBundle:
    bundle = EvidenceBundle()

    for item in data.get("parties", []):
        party = Party(
            party_id=item["party_id"],
            name=item["name"],
            role=PartyRole(item["role"]),
            license_no=item.get("license_no", ""),
        )
        bundle.parties[party.party_id] = party

    for item in data.get("relations", []):
        bundle.relations.append(
            Relation(
                relation_id=item["relation_id"],
                supplier_id=item["supplier_id"],
                pharmacy_id=item["pharmacy_id"],
                valid_from=_day(item["valid_from"]),
                valid_to=_day(item["valid_to"]) if item.get("valid_to") else None,
            )
        )

    for item in data.get("products", []):
        pid = item["product_id"]
        bundle.product_units[pid] = item["unit"]
        if item.get("per_box"):
            bundle.product_per_box[pid] = item["per_box"]

    for item in data.get("invoices", []):
        bundle.invoices.append(_parse_invoice(item, bundle))
    for item in data.get("delivery_notes", []):
        bundle.delivery_notes.append(_parse_delivery_note(item, bundle))
    for item in data.get("ledger", []):
        bundle.ledger.append(
            LedgerEntry(
                entry_id=item["entry_id"],
                pharmacy_id=item["pharmacy_id"],
                product_id=item["product_id"],
                change=item["change"],
                unit=item["unit"],
                entry_date=_day(item["entry_date"]),
                source_doc=item.get("source_doc", ""),
            )
        )
    for item in data.get("stock_counts", []):
        qty = parse_qty(
            item["qty"],
            bundle.product_units[item["product_id"]],
            bundle.product_per_box.get(item["product_id"]),
        )
        bundle.stock_counts.append(
            StockCount(
                count_id=item["count_id"],
                pharmacy_id=item["pharmacy_id"],
                product_id=item["product_id"],
                qty=qty,
                count_date=_day(item["count_date"]),
            )
        )
    for item in data.get("payments", []):
        bundle.payments.append(
            Payment(
                payment_id=item["payment_id"],
                payer_id=item["payer_id"],
                payee_id=item["payee_id"],
                amount_fen=item["amount_fen"],
                pay_date=_day(item["pay_date"]),
                invoice_ids=tuple(item.get("invoice_ids", [])),
            )
        )
    for item in data.get("returns", []):
        qty = parse_qty(
            item["qty"],
            bundle.product_units[item["product_id"]],
            bundle.product_per_box.get(item["product_id"]),
        )
        bundle.returns.append(
            ReturnNote(
                return_id=item["return_id"],
                invoice_id=item["invoice_id"],
                product_id=item["product_id"],
                qty=qty,
                return_date=_day(item["return_date"]),
            )
        )
    for item in data.get("allowances", []):
        bundle.allowances.append(
            Allowance(
                allowance_id=item["allowance_id"],
                invoice_id=item["invoice_id"],
                amount_fen=item["amount_fen"],
                allowance_date=_day(item["allowance_date"]),
            )
        )

    return bundle


def _parse_invoice(item: dict, bundle: EvidenceBundle) -> Invoice:
    lines = []
    for line in item["lines"]:
        pid = line["product_id"]
        qty = parse_qty(
            line["qty"],
            bundle.product_units[pid],
            bundle.product_per_box.get(pid),
        )
        lines.append(
            InvoiceLine(
                product_id=pid,
                qty=qty,
                unit_price_fen=line["unit_price_fen"],
                batch_no=line.get("batch_no", ""),
            )
        )
    return Invoice(
        invoice_id=item["invoice_id"],
        version=item["version"],
        supplier_id=item["supplier_id"],
        pharmacy_id=item["pharmacy_id"],
        issue_date=_day(item["issue_date"]),
        lines=tuple(lines),
        template_id=item.get("template_id", ""),
        payment_due=_day(item["payment_due"]) if item.get("payment_due") else None,
        supersedes=item.get("supersedes"),
    )


def _parse_delivery_note(item: dict, bundle: EvidenceBundle) -> DeliveryNote:
    lines = []
    for line in item["lines"]:
        pid = line["product_id"]
        qty = parse_qty(
            line["qty"],
            bundle.product_units[pid],
            bundle.product_per_box.get(pid),
        )
        lines.append(
            InvoiceLine(
                product_id=pid,
                qty=qty,
                unit_price_fen=line.get("unit_price_fen", 0),
                batch_no=line.get("batch_no", ""),
            )
        )
    return DeliveryNote(
        note_no=item["note_no"],
        invoice_id=item["invoice_id"],
        supplier_id=item["supplier_id"],
        pharmacy_id=item["pharmacy_id"],
        ship_date=_day(item["ship_date"]),
        lines=tuple(lines),
    )
