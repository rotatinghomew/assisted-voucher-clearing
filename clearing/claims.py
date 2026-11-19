"""供应方逐行申报与真实交付的核对。

分行或重复申报不增加实际交付；同一交付单位只能核一次。先核对有明确
凭证的部分，无法确认的份额留置（held），且被留置的交付份额不得再被
另一张结算单的申报领走。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .attribution import Attribution
from .facts import AppliedFacts
from .loader import Dataset
from .models import Claim

# 核对结论
ELIGIBLE = "eligible"            # 有真实交付支撑，可继续
OVERCLAIM = "overclaim"          # 超出该笔真实交付可核份额
SERVICE_MISMATCH = "service-mismatch"
PROVIDER_DISPUTE = "cross-provider-dispute"  # 跨供应方争议，人工解决
NO_DELIVERY = "nonexistent-delivery"         # 更正后交付不存在
UNSPECIFIED = "unspecified-delivery"         # 未指明交付，无法对应


@dataclass
class ClaimEvaluation:
    claim: Claim
    status: str
    eligible_units: int
    held_units: int
    rejected_units: int
    reasons: list[str] = field(default_factory=list)
    # 被占用的交付份额（含留置），key 为交付 ID
    delivery_usage: dict[str, int] = field(default_factory=dict)
    # 可作差额申请的金额（分），跨供应方争议不给自动差额
    adjustment_cents: int = 0


@dataclass
class ClaimReconciliation:
    evaluations: dict[str, ClaimEvaluation]
    # 每笔交付被申报占用的份额（已核 + 留置合并计，防止二次领取）
    reserved_by_delivery: dict[str, int]
    # 真实交付存在但没有任何同供应方申报认领的份数
    unclaimed_by_delivery: dict[str, int]
    terms: dict[str, int]


def evaluate_claims(data: Dataset, facts: AppliedFacts, attribution: Attribution) -> ClaimReconciliation:
    terms = {t.service: t.unit_cents for t in data.terms}
    reserved: dict[str, int] = {d.id: 0 for d in facts.deliveries.values()}
    evaluations: dict[str, ClaimEvaluation] = {}

    # 申报按 ID 确定顺序处理；同票服务共用额度不允许在几份结算单分别使用，
    # 因此每笔交付的占用在本循环内累计并即时封顶。
    for claim in sorted(data.claims, key=lambda c: c.id):
        delivery = facts.deliveries.get(claim.delivery_hint) if claim.delivery_hint else None
        evaluation = ClaimEvaluation(claim, ELIGIBLE, 0, 0, 0)

        if not claim.delivery_hint:
            evaluation.status = UNSPECIFIED
            evaluation.rejected_units = claim.units
            evaluation.reasons.append("delivery_hint 为空，无法对应真实交付")
        elif delivery is None or delivery.units <= 0:
            evaluation.status = NO_DELIVERY
            evaluation.rejected_units = claim.units
            evaluation.reasons.append("更正后该交付不存在或数量为 0")
        elif claim.service != delivery.service:
            evaluation.status = SERVICE_MISMATCH
            evaluation.rejected_units = claim.units
            evaluation.reasons.append(
                f"申报服务 {claim.service} 与交付服务 {delivery.service} 不一致"
            )
        elif claim.provider != delivery.provider:
            # 跨供应方争议：实际交付改属他人，历史付款不回写、不互抵。
            evaluation.status = PROVIDER_DISPUTE
            evaluation.rejected_units = claim.units
            evaluation.reasons.append(
                f"交付现属供应方 {delivery.provider}，申报方 {claim.provider}，需人工解决"
            )
        else:
            low, high = attribution.delivery_bounds[delivery.id]
            already = reserved[delivery.id]
            available = max(0, high - already)
            # 证实可核的份额直接确认；只能可能归属的份额留置。
            confirmable_now = max(0, low - already)
            wanted = claim.units
            granted = min(wanted, available)
            confirmed = min(granted, confirmable_now)
            held = granted - confirmed
            evaluation.eligible_units = confirmed
            evaluation.held_units = held
            evaluation.rejected_units = wanted - granted
            if granted:
                evaluation.delivery_usage[delivery.id] = granted
                reserved[delivery.id] += granted
            if evaluation.rejected_units:
                evaluation.status = OVERCLAIM
                evaluation.reasons.append(
                    f"交付 {delivery.id} 可核上界 {high} 份，已被其他申报占用 {already} 份"
                )
            elif held:
                evaluation.reasons.append(
                    f"{held} 份只有 possible 对应，留置待票间归属明确后再核"
                )
            evaluation.adjustment_cents = confirmed * terms.get(claim.service, 0)

        evaluations[claim.id] = evaluation

    unclaimed: dict[str, int] = {}
    for delivery_id, delivery in facts.deliveries.items():
        if delivery.units <= 0:
            continue
        _, high = attribution.delivery_bounds[delivery_id]
        leftover = max(0, high - reserved.get(delivery_id, 0))
        if leftover:
            unclaimed[delivery_id] = leftover

    return ClaimReconciliation(evaluations, reserved, unclaimed, terms)
