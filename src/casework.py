"""人工决定、整改追溯、说明权限隔离、票据模板串并。

边界：
- 自动匹配（matcher）只产出候选与差异；本模块记录的人工决定才是检查结论，
  且自动匹配永远不能覆盖已作出的决定。
- 决定只能由检查方追加；证据快照固定在作出决定时的材料版本，
  事后材料出现新版本时只提示"依据已更新"，不回改结论。
- 供应商/药店的说明：internal 仅提交方与检查方可见；
  shared_counterparty 可向所涉采购关系的相对方公开。
"""

from collections import defaultdict
from dataclasses import dataclass

from .contracts import ContractError, load_schema, validate
from .registry import MaterialRef, SubmissionRegistry


class CaseworkError(ValueError):
    """违反决定登记或权限规则。"""


@dataclass(frozen=True)
class Actor:
    party_id: str
    role: str  # inspector / supplier / pharmacy

    @classmethod
    def inspector(cls, party_id: str) -> "Actor":
        return cls(party_id, "inspector")


class Casework:
    def __init__(self, registry: SubmissionRegistry) -> None:
        self.registry = registry
        self._decisions: dict[str, dict] = {}
        self._remediations: dict[str, list[dict]] = defaultdict(list)

    # ------------------------------------------------------------- 人工决定

    def record_decision(self, decision: dict, actor: Actor) -> dict:
        if actor.role != "inspector":
            raise CaseworkError("只有检查方可以作出检查结论")
        validate(decision, load_schema("decision.schema.json"))
        decision_id = decision["decision_id"]
        if decision_id in self._decisions:
            raise CaseworkError(f"决定 {decision_id} 已存在，结论只能追加不能覆盖")

        # 证据快照必须指向真实存在的材料提交
        for snap in decision["evidence_snapshot"]:
            envelope = self.registry.get(snap["submission_id"])
            if (envelope["material_type"] != snap["material_type"]
                    or envelope["material_id"] != snap["material_id"]
                    or envelope["version"] != snap["version"]):
                raise CaseworkError(
                    f"证据快照 {snap['submission_id']} 与登记材料版本不一致"
                )
        self._decisions[decision_id] = decision
        for rem in decision.get("remediations", []):
            self._remediations[decision_id].append(dict(rem))
        return decision

    def get_decision(self, decision_id: str) -> dict:
        return self._decisions[decision_id]

    def decisions_for_diff(self, diff_id: str) -> list[dict]:
        return [d for d in self._decisions.values() if d["diff_id"] == diff_id]

    # ---------------------------------------------------------------- 整改

    def add_remediation(self, decision_id: str, remediation: dict, actor: Actor) -> None:
        if actor.role != "inspector":
            raise CaseworkError("只有检查方可以登记整改要求")
        if decision_id not in self._decisions:
            raise CaseworkError(f"决定 {decision_id} 不存在")
        required = {"remediation_id", "description", "due_date", "status"}
        missing = required - set(remediation)
        if missing:
            raise CaseworkError(f"整改记录缺少字段 {sorted(missing)}")
        if remediation["status"] not in {"open", "in_progress", "closed"}:
            raise CaseworkError("整改状态非法")
        ids = {r["remediation_id"] for r in self._remediations[decision_id]}
        if remediation["remediation_id"] in ids:
            raise CaseworkError("整改编号重复")
        self._remediations[decision_id].append(dict(remediation))

    def update_remediation(self, decision_id: str, remediation_id: str,
                           status: str, closed_at: str | None, actor: Actor) -> None:
        """整改状态变更采用追加事件：保留原值与全部历史。"""
        if actor.role != "inspector":
            raise CaseworkError("只有检查方可以变更整改状态")
        for rem in self._remediations[decision_id]:
            if rem["remediation_id"] == remediation_id:
                rem.setdefault("history", []).append(
                    {"status": rem["status"], "closed_at": rem.get("closed_at")})
                rem["status"] = status
                rem["closed_at"] = closed_at
                return
        raise CaseworkError(f"整改 {remediation_id} 不存在")

    def remediations(self, decision_id: str) -> list[dict]:
        return self._remediations.get(decision_id, [])

    # ----------------------------------------------------------- 差异追溯链

    def trace(self, result: dict, diff_id: str) -> dict:
        """从任一差异回到：四类原始依据版本、人工决定、整改、依据更新提示。"""
        diff = next((d for cand in result["candidates"]
                     for d in cand["differences"] if d["diff_id"] == diff_id), None)
        if diff is None:
            raise CaseworkError(f"本次核验结果中找不到差异 {diff_id}")

        evidence = []
        for ref_dict in diff["evidence"]:
            ref = MaterialRef(ref_dict["material_type"], ref_dict["material_id"],
                              ref_dict["version"], ref_dict["submission_id"])
            envelope = self.registry.get(ref.submission_id)
            evidence.append({
                "ref": ref.as_dict(),
                "submitted_by": envelope["submitted_by"],
                "submitted_at": envelope["submitted_at"],
                "has_newer_version": self.registry.has_newer_versions(ref),
                "version_history": [
                    {"version": e["version"], "submission_id": e["submission_id"],
                     "submitted_at": e["submitted_at"]}
                    for e in self.registry.versions(ref.material_type, ref.material_id)
                ],
            })

        decisions = []
        for d in self.decisions_for_diff(diff_id):
            decisions.append({
                "decision_id": d["decision_id"],
                "conclusion": d["conclusion"],
                "decided_by": d["decided_by"],
                "decided_at": d["decided_at"],
                "rationale": d["rationale"],
                "evidence_snapshot": d["evidence_snapshot"],
                "snapshot_stale": any(
                    self.registry.has_newer_versions(
                        MaterialRef(s["material_type"], s["material_id"],
                                    s["version"], s["submission_id"]))
                    for s in d["evidence_snapshot"]
                ),
                "remediations": self.remediations(d["decision_id"]),
            })

        return {"diff": diff, "evidence": evidence, "decisions": decisions}

    # ------------------------------------------------------------- 说明权限

    def visible_explanations(self, actor: Actor, case_id: str) -> list[dict]:
        out = []
        for env in self.registry.latest_all("explanation"):
            if env["case_id"] != case_id:
                continue
            if self._can_see(env, actor):
                out.append(env)
        return out

    def _can_see(self, envelope: dict, actor: Actor) -> bool:
        owner = envelope["submitted_by"]["party_id"]
        if actor.role == "inspector":
            return True
        if actor.party_id == owner:
            return True
        if envelope.get("visibility", "internal") != "shared_counterparty":
            return False
        # 仅采购相对方：说明所涉发票上的销方或购方
        content = envelope["content"]
        counterparties = self._counterparties_of(content["regarding_refs"])
        return actor.party_id in counterparties

    def _counterparties_of(self, refs: list[str]) -> set[str]:
        parties = set()
        for env in self.registry.latest_all("invoice_pack"):
            inv = env["content"]
            touched = {inv["invoice_number"], inv["invoice_id"]}
            for note in inv.get("delivery_notes", []):
                touched.add(note["note_number"])
            for doc in inv.get("return_documents", []):
                touched.add(doc["doc_number"])
            if set(refs) & touched:
                parties.add(inv["seller"]["party_id"])
                parties.add(inv["buyer"]["party_id"])
        return parties

    # ----------------------------------------------------------- 模板串并

    def template_clusters(self, min_stores: int = 2) -> list[dict]:
        """同一票据模板在不同门店反复出现的聚类。"""
        groups: dict[str, dict] = defaultdict(
            lambda: {"stores": set(), "pharmacies": set(), "sellers": set(),
                     "invoices": []})
        for env in self.registry.latest_all("invoice_pack"):
            inv = env["content"]
            g = groups[inv["template_id"]]
            g["stores"].add(inv["store_id"])
            g["pharmacies"].add(inv["pharmacy_id"])
            g["sellers"].add(inv["seller"]["party_id"])
            g["invoices"].append({
                "invoice_number": inv["invoice_number"],
                "invoice_date": inv["invoice_date"],
                "store_id": inv["store_id"],
                "pharmacy_id": inv["pharmacy_id"],
                "seller_id": inv["seller"]["party_id"],
                "case_id": env["case_id"],
            })

        clusters = []
        for template_id, g in sorted(groups.items()):
            if len(g["stores"]) >= min_stores:
                clusters.append({
                    "template_id": template_id,
                    "store_count": len(g["stores"]),
                    "pharmacy_count": len(g["pharmacies"]),
                    "seller_count": len(g["sellers"]),
                    "recurring_pattern": len(g["sellers"]) == 1
                                        and len(g["stores"]) > len(g["sellers"]),
                    "stores": sorted(g["stores"]),
                    "sellers": sorted(g["sellers"]),
                    "invoices": sorted(g["invoices"],
                                       key=lambda x: (x["invoice_date"],
                                                      x["invoice_number"])),
                })
        return clusters
