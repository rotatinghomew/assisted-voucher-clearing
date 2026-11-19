"""历史结算账、更正定性与差额申请。

更正使一笔已支付交付改属后，不回写付款，也不能再付一遍。需要区分三类：
跨供应方争议（无互抵资格，人工解决）、同一供应方的科目/票间变更（按
``terms`` 允许范围形成差额申请）、实际不存在的交付（不得申请差额）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .claims import ClaimReconciliation, NO_DELIVERY, PROVIDER_DISPUTE
from .facts import AppliedFacts
from .loader import Dataset
from .models import Payment

CROSS_PROVIDER = "cross-provider-dispute"
SAME_PROVIDER = "same-provider-reallocation"
VANISHED_DELIVERY = "vanished-delivery"

# 结算三桶，禁止合并成一个“剩余额度”
SETTLED = "settled"          # 已结算行
PENDING = "pending-review"   # 待核对行（留置或歧义）
UNCLAIMED = "unclaimed"      # 无人申领的已交付量


@dataclass
class PaymentCase:
    payment: Payment
    kind: str
    claim_id: str
    explanation: str
    # 差额申请金额（分）；无资格时为 0
    difference_cents: int = 0
    eligible_for_application: bool = False


@dataclass
class CorrectionCase:
    correction_id: str
    kind: str
    supersedes: str
    detail: str
    difference_cents: int = 0
    eligible_for_application: bool = False
    affected_pending_items: list[str] = field(default_factory=list)
    independently_settleable: list[str] = field(default_factory=list)
    # 票间更正若挂靠在一笔已发生跨供应方争议的交付上，差额申请须等争议解决
    linked_disputes: list[str] = field(default_factory=list)


@dataclass
class SettlementLedger:
    payment_cases: list[PaymentCase]
    correction_cases: list[CorrectionCase]
    # 三桶统计：key 为（供应方, 服务），值为份数
    buckets: dict[str, dict[tuple[str, str], int]]
    # 可独立继续结算的申报；受影响未送审项目
    blocked_unsent: list[str]
    applications: list[CorrectionCase]


def _provider_before(data: Dataset, delivery_id: str) -> str:
    original = next(d for d in data.deliveries if d.id == delivery_id)
    return original.provider


def build_ledger(
    data: Dataset,
    facts: AppliedFacts,
    claims_recon: ClaimReconciliation,
) -> SettlementLedger:
    payment_cases: list[PaymentCase] = []
    correction_cases: list[CorrectionCase] = []

    claim_by_id = {c.id: c for c in data.claims}
    terms = {t.service: t for t in data.terms}

    # 先给每条更正定性。
    for correction in facts.corrections:
        target = correction.supersedes
        if facts.kind.get(correction.id) == "delivery-provider":
            current = facts.deliveries[target]
            before = _provider_before(data, target)
            term = terms.get(current.service)
            allowed = bool(term and term.same_provider_adjustment and before == current.provider)
            # 供应方实际发生变化 => 跨供应方，terms 中 cross_provider_offset=no
            case = CorrectionCase(
                correction_id=correction.id,
                kind=CROSS_PROVIDER,
                supersedes=target,
                detail=f"交付 {target} 供应方 {before} -> {current.provider}",
                eligible_for_application=False,
            )
            case.affected_pending_items = [
                c.id for c in data.claims
                if c.delivery_hint == target and c.id not in {p.claim for p in data.payments}
            ]
            # 同笔交付上与争议无关的其他项目仍可独立结算。
            case.independently_settleable = [
                c.id for c in data.claims
                if c.delivery_hint != target
                and claims_recon.evaluations[c.id].eligible_units > 0
            ]
            correction_cases.append(case)
        elif facts.kind.get(correction.id) == "delivery-units":
            current = facts.deliveries[target]
            case = CorrectionCase(
                correction_id=correction.id,
                kind=VANISHED_DELIVERY if current.units == 0 else SAME_PROVIDER,
                supersedes=target,
                detail=f"交付 {target} 数量更正为 {current.units}",
                eligible_for_application=False,  # 不存在的交付不形成差额申请
            )
            case.affected_pending_items = [
                c.id for c in data.claims if c.delivery_hint == target
            ]
            case.independently_settleable = [
                c.id for c in data.claims
                if c.delivery_hint != target
                and claims_recon.evaluations[c.id].eligible_units > 0
            ]
            correction_cases.append(case)
        else:
            # 票间/科目变更：同一供应方范围内，按 terms 允许形成差额申请。
            relation = facts.relations[target]
            delivery = facts.deliveries[relation.delivery]
            term = terms.get(delivery.service)
            eligible = bool(term and term.same_provider_adjustment)
            linked = [
                c.id for c in facts.corrections
                if facts.kind.get(c.id) == "delivery-provider"
                and c.supersedes == relation.delivery
            ]
            case = CorrectionCase(
                correction_id=correction.id,
                kind=SAME_PROVIDER,
                supersedes=target,
                detail=f"对应 {target} 改挂票 {relation.voucher}（供应方 {delivery.provider}）",
                difference_cents=relation.max_units * term.unit_cents if (eligible and term) else 0,
                eligible_for_application=eligible,
                linked_disputes=linked,
            )
            correction_cases.append(case)

    # 历史付款逐笔定性：付款不回写、不重复支付。
    paid_claims = {p.claim for p in data.payments}
    for payment in sorted(data.payments, key=lambda p: p.id):
        claim = claim_by_id[payment.claim]
        evaluation = claims_recon.evaluations[payment.claim]
        if evaluation.status == PROVIDER_DISPUTE:
            case = PaymentCase(
                payment, CROSS_PROVIDER, claim.id,
                "付款对应交付已改属另一供应方，付款保留不回写，无互抵资格，人工解决",
            )
        elif evaluation.status == NO_DELIVERY:
            case = PaymentCase(
                payment, VANISHED_DELIVERY, claim.id,
                "付款对应交付经更正不存在，不得再付，走人工追回而非差额申请",
            )
        else:
            case = PaymentCase(
                payment, SAME_PROVIDER, claim.id,
                "票间归属更正不回写付款，原支付继续有效",
            )
        payment_cases.append(case)

    # 三桶统计，按（供应方, 服务）汇总，严格分桶。
    buckets: dict[str, dict[tuple[str, str], int]] = {
        SETTLED: {}, PENDING: {}, UNCLAIMED: {}
    }

    def add(bucket: str, provider: str, service: str, units: int) -> None:
        if units <= 0:
            return
        key = (provider, service)
        buckets[bucket][key] = buckets[bucket].get(key, 0) + units

    for claim in data.claims:
        evaluation = claims_recon.evaluations[claim.id]
        if claim.id in paid_claims:
            # 历史已支付的份额进已结算桶（即使后续产生争议，也不挪桶）。
            paid_units = sum(p.units for p in data.payments if p.claim == claim.id)
            add(SETTLED, claim.provider, claim.service, paid_units)
            continue
        add(PENDING, claim.provider, claim.service, evaluation.eligible_units + evaluation.held_units)

    for delivery_id, units in claims_recon.unclaimed_by_delivery.items():
        delivery = facts.deliveries[delivery_id]
        add(UNCLAIMED, delivery.provider, delivery.service, units)

    blocked = sorted({
        item
        for case in correction_cases
        for item in case.affected_pending_items
    })
    applications = [c for c in correction_cases if c.eligible_for_application]

    return SettlementLedger(
        payment_cases=payment_cases,
        correction_cases=correction_cases,
        buckets=buckets,
        blocked_unsent=sorted(set(blocked)),
        applications=applications,
    )
