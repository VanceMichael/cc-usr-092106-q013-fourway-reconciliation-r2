"""一次检查的案卷：人工决定与整改溯源。

自动匹配只产生候选和差异，任何差异都不是结论；结论只能由检查人员
逐条作出。决定、整改同样只追加，决定时看到的证据版本被一并冻结，
日后发票追加了新版本，也能回到“当时依据的是哪一版”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .engine import Difference, EvidenceRef, MatchCandidate, TemplateCluster
from .models import EvidenceKind, Explanation
from .permissions import Role, Viewer, filter_explanations
from .store import EvidenceStore


class DecisionKind(str, Enum):
    CONFIRMED = "confirmed"          # 认定问题
    ACCEPTED_EXPLANATION = "accepted_explanation"  # 情况说明成立
    NEEDS_FOLLOWUP = "needs_followup"  # 需进一步核实
    DISMISSED = "dismissed"          # 排除


class RectificationStatus(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ManualDecision:
    decision_id: str
    difference_id: str
    kind: DecisionKind
    rationale: str
    decided_by: str
    decided_at: date
    # 决定时该差异引用的证据版本快照
    evidence_snapshot: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class Rectification:
    rectification_id: str
    difference_id: str
    requirement: str
    due_date: date
    status: RectificationStatus
    created_by: str
    created_at: date
    completed_at: date | None = None
    completion_note: str = ""


@dataclass
class InspectionCase:
    case_id: str
    title: str
    opened_at: date
    closed_at: date | None = None
    decisions: list[ManualDecision] = field(default_factory=list)
    rectifications: list[Rectification] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None

    def decisions_for(self, difference_id: str) -> list[ManualDecision]:
        return [d for d in self.decisions if d.difference_id == difference_id]

    def latest_decision(self, difference_id: str) -> ManualDecision | None:
        rows = self.decisions_for(difference_id)
        return rows[-1] if rows else None

    def rectifications_for(self, difference_id: str) -> list[Rectification]:
        return [r for r in self.rectifications if r.difference_id == difference_id]


class CaseError(ValueError):
    """案卷操作不合法，如非检查人员下结论、对已结案卷追加决定。"""


_INSPECTOR_ROLES = {Role.INSPECTOR, Role.LEAD}


def _require_inspector(viewer: Viewer) -> None:
    if viewer.role not in _INSPECTOR_ROLES:
        raise CaseError("自动匹配与外部账号无权作出检查结论")


def record_decision(
    case: InspectionCase,
    difference: Difference,
    kind: DecisionKind,
    rationale: str,
    viewer: Viewer,
    decided_at: date,
) -> ManualDecision:
    _require_inspector(viewer)
    if case.is_closed:
        raise CaseError(f"案卷 {case.case_id} 已关闭，结论变更请重开案卷")
    decision = ManualDecision(
        decision_id=f"DEC-{len(case.decisions) + 1:04d}",
        difference_id=difference.difference_id,
        kind=kind,
        rationale=rationale,
        decided_by=viewer.party_id or viewer.role.value,
        decided_at=decided_at,
        evidence_snapshot=tuple(difference.refs),
    )
    case.decisions.append(decision)
    return decision


def open_rectification(
    case: InspectionCase,
    difference: Difference,
    requirement: str,
    due_date: date,
    viewer: Viewer,
    created_at: date,
) -> Rectification:
    _require_inspector(viewer)
    if case.is_closed:
        raise CaseError(f"案卷 {case.case_id} 已关闭")
    item = Rectification(
        rectification_id=f"RECT-{len(case.rectifications) + 1:04d}",
        difference_id=difference.difference_id,
        requirement=requirement,
        due_date=due_date,
        status=RectificationStatus.OPEN,
        created_by=viewer.party_id or viewer.role.value,
        created_at=created_at,
    )
    case.rectifications.append(item)
    return item


def complete_rectification(
    case: InspectionCase,
    rectification_id: str,
    note: str,
    viewer: Viewer,
    completed_at: date,
) -> Rectification:
    _require_inspector(viewer)
    for i, item in enumerate(case.rectifications):
        if item.rectification_id != rectification_id:
            continue
        if item.status == RectificationStatus.COMPLETED:
            raise CaseError(f"整改 {rectification_id} 已完成，更正请追加记录")
        done = Rectification(
            rectification_id=item.rectification_id,
            difference_id=item.difference_id,
            requirement=item.requirement,
            due_date=item.due_date,
            status=RectificationStatus.COMPLETED,
            created_by=item.created_by,
            created_at=item.created_at,
            completed_at=completed_at,
            completion_note=note,
        )
        case.rectifications[i] = done
        return done
    raise CaseError(f"整改 {rectification_id} 不存在")


def close_case(case: InspectionCase, viewer: Viewer, closed_at: date) -> None:
    if viewer.role != Role.LEAD:
        raise CaseError("只有检查负责人可以关闭案卷")
    case.closed_at = closed_at


@dataclass(frozen=True)
class EvidenceBacking:
    """一条差异回到四类原始依据的落点。"""

    invoice: tuple[EvidenceRef, ...]
    delivery_note: tuple[EvidenceRef, ...]
    ledger: tuple[EvidenceRef, ...]
    stock_count: tuple[EvidenceRef, ...]
    payment: tuple[EvidenceRef, ...]
    supporting: tuple[EvidenceRef, ...]  # 退货单、折让单等解释性材料

    def as_dict(self) -> dict[str, tuple[EvidenceRef, ...]]:
        return {
            "invoice": self.invoice,
            "delivery_note": self.delivery_note,
            "ledger": self.ledger,
            "stock_count": self.stock_count,
            "payment": self.payment,
            "supporting": self.supporting,
        }


_BACKING_KINDS = {
    "invoice": EvidenceKind.INVOICE,
    "delivery_note": EvidenceKind.DELIVERY_NOTE,
    "ledger": EvidenceKind.LEDGER,
    "stock_count": EvidenceKind.STOCK_COUNT,
    "payment": EvidenceKind.PAYMENT,
}


def evidence_backing(difference: Difference) -> EvidenceBacking:
    buckets: dict[str, list[EvidenceRef]] = {key: [] for key in _BACKING_KINDS}
    supporting: list[EvidenceRef] = []
    for ref in difference.refs:
        for key, kind in _BACKING_KINDS.items():
            if ref.kind == kind:
                buckets[key].append(ref)
                break
        else:
            supporting.append(ref)
    return EvidenceBacking(
        invoice=tuple(buckets["invoice"]),
        delivery_note=tuple(buckets["delivery_note"]),
        ledger=tuple(buckets["ledger"]),
        stock_count=tuple(buckets["stock_count"]),
        payment=tuple(buckets["payment"]),
        supporting=tuple(supporting),
    )


@dataclass(frozen=True)
class Trace:
    """从差异出发的完整溯源链。"""

    difference: Difference
    backing: EvidenceBacking
    explanations: tuple[Explanation, ...]
    decisions: tuple[ManualDecision, ...]
    rectifications: tuple[Rectification, ...]
    candidate: MatchCandidate | None = None
    template_cluster: TemplateCluster | None = None


def build_trace(
    difference: Difference,
    case: InspectionCase,
    store: EvidenceStore,
    viewer: Viewer,
    candidates: list[MatchCandidate] | None = None,
    clusters: list[TemplateCluster] | None = None,
) -> Trace:
    explanations = filter_explanations(
        [e for e in store.explanations() if e.difference_id == difference.difference_id],
        viewer,
    )
    candidate = None
    if candidates:
        for cand in candidates:
            inv_refs = {r for r in difference.refs if r.kind == EvidenceKind.INVOICE}
            if any(r.ref_id in cand.invoice_key for r in inv_refs):
                candidate = cand
                break
    cluster = None
    if clusters:
        inv_ids = {r.ref_id for r in difference.refs if r.kind == EvidenceKind.INVOICE}
        for cl in clusters:
            if any(key.rsplit("@", 1)[0] in inv_ids for key in cl.invoice_keys):
                cluster = cl
                break
    return Trace(
        difference=difference,
        backing=evidence_backing(difference),
        explanations=tuple(explanations),
        decisions=tuple(case.decisions_for(difference.difference_id)),
        rectifications=tuple(case.rectifications_for(difference.difference_id)),
        candidate=candidate,
        template_cluster=cluster,
    )
