"""审核视角：真实利用率、候选凭证追溯与可送审计划。

审核员按采购点（issuer）和服务类型查看利用率，并能追到形成某一范围的
候选凭证、争议金额与付款日期。更正与部分回执先后到达时，回答三件事：
现在可以送哪部分、哪些在对方账上仍未定、哪些要按差额复核。
"""

from __future__ import annotations

from dataclasses import dataclass

from .attribution import Attribution
from .claims import ClaimReconciliation
from .facts import AppliedFacts
from .loader import Dataset
from .settlement import SettlementLedger
from .transmissions import REJECTED, SENT, TransmissionBoard


@dataclass
class UtilizationRow:
    issuer: str
    service: str
    capacity: int
    consumed_lower: int
    consumed_upper: int
    utilization_lower: float
    utilization_upper: float
    candidate_vouchers: list[str]
    candidate_relations: list[str]


@dataclass
class SubmissionPlanItem:
    claim: str
    provider: str
    service: str
    units: int
    suggested_voucher: str
    unit_cents: int
    amount_cents: int
    basis: str


@dataclass
class HeldItem:
    claim: str
    provider: str
    service: str
    units: int
    reason: str
    candidate_vouchers: list[str]


@dataclass
class ReleasableItem:
    claim: str
    units: int
    returned_from_line: str
    note: str


@dataclass
class AuditView:
    utilization: list[UtilizationRow]
    open_items: list[dict]
    difference_review: list[dict]
    submission_plan: list[SubmissionPlanItem]
    held_items: list[HeldItem]
    releasable_items: list[ReleasableItem]
    trace: dict[str, dict]
    totals: dict


def _candidate_relations(data: Dataset, facts: AppliedFacts, issuer: str, service: str) -> list[str]:
    voucher_ids = {v.id for v in data.vouchers if v.issuer == issuer and v.service == service}
    return sorted(
        r.id for r in facts.relations.values()
        if r.voucher in voucher_ids and r.state != "excluded"
    )


def build_utilization(
    data: Dataset, facts: AppliedFacts, attribution: Attribution
) -> list[UtilizationRow]:
    ranges = attribution.range_for_issuer_service(data.vouchers)
    rows: list[UtilizationRow] = []
    grouped: dict[tuple[str, str], list[str]] = {}
    for voucher in data.vouchers:
        grouped.setdefault((voucher.issuer, voucher.service), []).append(voucher.id)
    for (issuer, service), voucher_ids in sorted(grouped.items()):
        capacity = sum(next(v.units for v in data.vouchers if v.id == vid) for vid in voucher_ids)
        low, high = ranges[(issuer, service)]
        rows.append(
            UtilizationRow(
                issuer=issuer,
                service=service,
                capacity=capacity,
                consumed_lower=low,
                consumed_upper=high,
                utilization_lower=round(low / capacity, 4) if capacity else 0.0,
                utilization_upper=round(high / capacity, 4) if capacity else 0.0,
                candidate_vouchers=sorted(voucher_ids),
                candidate_relations=_candidate_relations(data, facts, issuer, service),
            )
        )
    return rows


def build_submission_plan(
    data: Dataset,
    facts: AppliedFacts,
    attribution: Attribution,
    claims_recon: ClaimReconciliation,
    board: TransmissionBoard,
    ledger: SettlementLedger,
) -> list[SubmissionPlanItem]:
    """按当前票间余量与申报可核份额，给出可立即送审的确定性计划。"""
    paid_claims = {p.claim for p in data.payments}
    sent_units: dict[str, int] = {}
    pending_on_other_ledger: set[str] = set()
    for batch in board.batches.values():
        for line in batch.lines.values():
            if line.status in (SENT,):
                pending_on_other_ledger.add(line.transmission.claim)
            if line.status != REJECTED:
                sent_units[line.transmission.claim] = (
                    sent_units.get(line.transmission.claim, 0) + line.transmission.units
                )

    # 票上已被报送/付费占用的余量（按 allocation 记账）。
    voucher_used: dict[str, int] = {v.id: 0 for v in data.vouchers}
    for batch in board.batches.values():
        for line in batch.lines.values():
            if line.status != REJECTED:
                voucher_used[line.transmission.allocation] = (
                    voucher_used.get(line.transmission.allocation, 0) + line.transmission.units
                )
    # 历史 payments 的 allocation 同样占票。
    for payment in data.payments:
        voucher_used[payment.allocation] = voucher_used.get(payment.allocation, 0) + payment.units

    plan: list[SubmissionPlanItem] = []
    blocked = set(ledger.blocked_unsent) | set(board.frozen_claims)
    for claim in sorted(data.claims, key=lambda c: c.id):
        if claim.id in paid_claims or claim.id in blocked or claim.id in pending_on_other_ledger:
            continue
        evaluation = claims_recon.evaluations[claim.id]
        available_claim = evaluation.eligible_units
        if available_claim <= 0:
            continue
        already_sent = sent_units.get(claim.id, 0)
        wanted = max(0, available_claim - already_sent)
        if wanted <= 0:
            continue
        # 在该交付的候选票里，选一张仍有余量的票；按票 ID 确定顺序。
        delivery_id = claim.delivery_hint
        candidate_vouchers = _live_candidate_vouchers(data, facts, attribution, delivery_id)
        chosen = None
        for voucher_id in candidate_vouchers:
            capacity = next(v.units for v in data.vouchers if v.id == voucher_id)
            headroom = capacity - voucher_used.get(voucher_id, 0)
            if headroom > 0:
                chosen = voucher_id
                break
        if chosen is None:
            continue
        units = min(wanted, next(v.units for v in data.vouchers if v.id == chosen) - voucher_used[chosen])
        if units <= 0:
            continue
        unit_price = claims_recon.terms.get(claim.service, 0)
        voucher_used[chosen] += units
        plan.append(
            SubmissionPlanItem(
                claim=claim.id,
                provider=claim.provider,
                service=claim.service,
                units=units,
                suggested_voucher=chosen,
                unit_cents=unit_price,
                amount_cents=units * unit_price,
                basis=f"交付 {delivery_id} 的可核份额，候选票 {candidate_vouchers}",
            )
        )
    return plan


def _live_candidate_vouchers(data, facts, attribution, delivery_id: str) -> list[str]:
    """一笔交付当前仍可挂的票（剔除排除项与作废约束）。"""
    barred_relations = {b.relation.id for b in attribution.barred}
    return sorted({
        r.voucher for r in facts.relations.values()
        if r.delivery == delivery_id
        and r.state != "excluded"
        and r.id not in barred_relations
    })


def build_held_items(
    data: Dataset,
    facts: AppliedFacts,
    attribution: Attribution,
    claims_recon: ClaimReconciliation,
    board: TransmissionBoard,
    ledger: SettlementLedger,
) -> list[HeldItem]:
    """只有 possible 对应、无法唯一归属的份额：可留置但不能再被别处领走。"""
    paid_claims = {p.claim for p in data.payments}
    on_other_ledger = {
        line.transmission.claim
        for batch in board.batches.values()
        for line in batch.lines.values()
    }
    blocked = set(ledger.blocked_unsent) | set(board.frozen_claims)
    items: list[HeldItem] = []
    for claim in sorted(data.claims, key=lambda c: c.id):
        if claim.id in paid_claims or claim.id in on_other_ledger or claim.id in blocked:
            continue
        evaluation = claims_recon.evaluations[claim.id]
        if evaluation.held_units <= 0:
            continue
        items.append(
            HeldItem(
                claim=claim.id,
                provider=claim.provider,
                service=claim.service,
                units=evaluation.held_units,
                reason="仅有 possible 票据对应，票间归属未唯一，留置待证",
                candidate_vouchers=_live_candidate_vouchers(
                    data, facts, attribution, claim.delivery_hint
                ),
            )
        )
    return items


def build_releasable_items(data: Dataset, board: TransmissionBoard) -> list[ReleasableItem]:
    """驳回释放的未付款份额，并说明当前证据下是否仍合资格。"""
    items: list[ReleasableItem] = []
    for batch_id, batch in sorted(board.batches.items()):
        for line_id, line in sorted(batch.lines.items()):
            if line.status != REJECTED:
                continue
            claim_id = line.transmission.claim
            if claim_id in board.releasable:
                note = "驳回只释放未付款份额；当前证据仍合资格，可随新批次再送"
            else:
                note = "驳回释放份额，但新证据下已不合资格，不得再送"
            items.append(
                ReleasableItem(
                    claim=claim_id,
                    units=line.rejected_units,
                    returned_from_line=line_id,
                    note=note,
                )
            )
    return items


def build_audit_view(
    data: Dataset,
    facts: AppliedFacts,
    attribution: Attribution,
    claims_recon: ClaimReconciliation,
    board: TransmissionBoard,
    ledger: SettlementLedger,
) -> AuditView:
    utilization = build_utilization(data, facts, attribution)
    plan = build_submission_plan(data, facts, attribution, claims_recon, board, ledger)
    held = build_held_items(data, facts, attribution, claims_recon, board, ledger)
    releasable = build_releasable_items(data, board)

    open_items: list[dict] = []
    for batch_id, batch in sorted(board.batches.items()):
        for line_id, line in sorted(batch.lines.items()):
            if line.status == SENT:
                open_items.append({
                    "batch": batch_id,
                    "line": line_id,
                    "claim": line.transmission.claim,
                    "units": line.transmission.units,
                    "allocation": line.transmission.allocation,
                    "status": line.status,
                    "note": "整批已 received 但无逐行回执，不能视为驳回",
                })

    difference_review: list[dict] = []
    payment_by_claim = {p.claim: p for p in data.payments}
    for case in ledger.correction_cases:
        entry = {
            "correction": case.correction_id,
            "kind": case.kind,
            "detail": case.detail,
            "eligible_for_application": case.eligible_for_application,
            "difference_cents": case.difference_cents,
            "affected_pending_items": case.affected_pending_items,
            "independently_settleable": case.independently_settleable,
            "linked_disputes": case.linked_disputes,
        }
        if case.kind == "cross-provider-dispute":
            payment = payment_by_claim.get(
                next((c.id for c in data.claims if c.delivery_hint == case.supersedes), "")
            )
            if payment:
                entry["disputed_cents"] = payment.cents
                entry["payment_date"] = payment.paid
        difference_review.append(entry)

    # 追溯：每个（采购点, 服务）范围对应哪些凭证、争议与付款日期。
    # 更正会把关系改挂到其他采购点的票，因此同时保留原始资料里的候选关系。
    original_relations_by_voucher: dict[str, list[str]] = {}
    for relation in data.relations:
        original_relations_by_voucher.setdefault(relation.voucher, []).append(relation.id)
    barred_pairs = {(b.relation.delivery, b.relation.voucher): b.reason for b in attribution.barred}
    trace: dict[str, dict] = {}
    for row in utilization:
        key = f"{row.issuer}/{row.service}"
        relation_ids = row.candidate_relations
        original_relation_ids = sorted({
            rid
            for voucher_id in row.candidate_vouchers
            for rid in original_relations_by_voucher.get(voucher_id, [])
        })
        all_relation_ids = sorted(set(relation_ids) | set(original_relation_ids))
        delivery_ids = sorted({
            facts.relations[r].delivery for r in all_relation_ids if r in facts.relations
        })
        disputes = [
            c.correction_id for c in ledger.correction_cases
            if c.kind == "cross-provider-dispute" and c.supersedes in delivery_ids
        ]
        payment_dates = sorted({
            p.paid for p in data.payments
            if next((c for c in data.claims if c.id == p.claim), None)
            and next(c for c in data.claims if c.id == p.claim).service == row.service
        })
        barred_here = [
            {"relation": r, "reason": barred_pairs[(facts.relations[r].delivery, facts.relations[r].voucher)]}
            for r in all_relation_ids
            if (facts.relations[r].delivery, facts.relations[r].voucher) in barred_pairs
        ]
        trace[key] = {
            "vouchers": row.candidate_vouchers,
            "relations": relation_ids,
            "original_relations": original_relation_ids,
            "deliveries": delivery_ids,
            "disputed_corrections": disputes,
            "payment_dates": payment_dates,
            "consumed_bounds": (row.consumed_lower, row.consumed_upper),
            "barred_relations": barred_here,
        }

    totals = {
        "submission_units": sum(item.units for item in plan),
        "submission_cents": sum(item.amount_cents for item in plan),
        "held_units": sum(item.units for item in held),
        "releasable_units": sum(item.units for item in releasable),
        "paid_on_other_ledger_cents": sum(
            line.paid_cents
            for batch in board.batches.values()
            for line in batch.lines.values()
        ),
        "open_line_count": len(open_items),
        "frozen_claims": sorted(board.frozen_claims),
    }
    return AuditView(
        utilization, open_items, difference_review, plan,
        held, releasable, trace, totals,
    )
