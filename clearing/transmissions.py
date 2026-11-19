"""逐行报送、回执流水与等待期冻结。

规则：
* 对方可先整批 received，随后只确认部分行；没有逐行回执不是驳回。
* 同一回执 id 重复到达不重复增加已付量。
* 确认驳回只释放对应未付款份额；是否还能再送要看新证据下是否仍合资格。
* 送审资格被撤回不释放已经送出的份额。
* 等待回执期间发现原票归属不实：尚未送审的行冻结，待回执明确后再区分
  待修正与确已付款的差额。
* 全部资料为本地数据，不接银行或真实采购系统。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .claims import ClaimReconciliation
from .facts import AppliedFacts
from .loader import Dataset
from .models import Receipt, Transmission

SENT = "sent"                # 已送出，无逐行回执
PAID = "paid"                # 逐行付费确认
REJECTED = "rejected"        # 逐行驳回
FROZEN = "frozen"            # 尚未送审，因归属不实被冻结


@dataclass
class LineState:
    transmission: Transmission
    status: str
    paid_units: int
    paid_cents: int
    rejected_units: int
    # 指向生效的回执 id（重复回执不另计）
    receipt_id: str | None
    events: list[Receipt] = field(default_factory=list)


@dataclass
class BatchState:
    batch: str
    received: bool
    lines: dict[str, LineState]


@dataclass
class TransmissionBoard:
    batches: dict[str, BatchState]
    # 被驳回释放、但在当前证据下仍可再送审的份额（claim -> units）
    releasable: dict[str, int]
    # 尚未送审即被冻结的申报
    frozen_claims: dict[str, str]
    as_of: str | None


def _receipts_until(data: Dataset, as_of: str | None) -> list[Receipt]:
    rows = data.receipts if as_of is None else [r for r in data.receipts if r.recorded <= as_of]
    return sorted(rows, key=lambda r: (r.recorded, r.id, r.line or ""))


def _transmissions_until(data: Dataset, as_of: str | None) -> list[Transmission]:
    rows = (
        data.transmissions
        if as_of is None
        else [t for t in data.transmissions if t.sent <= as_of]
    )
    return sorted(rows, key=lambda t: (t.sent, t.id))


def build_board(
    data: Dataset,
    facts: AppliedFacts,
    claims_recon: ClaimReconciliation,
    as_of: str | None = None,
    frozen_claim_ids: set[str] | None = None,
) -> TransmissionBoard:
    frozen_claim_ids = frozen_claim_ids or set()
    batches: dict[str, BatchState] = {}

    for transmission in _transmissions_until(data, as_of):
        batch = batches.setdefault(
            transmission.batch, BatchState(transmission.batch, False, {})
        )
        batch.lines[transmission.id] = LineState(
            transmission=transmission,
            status=SENT,
            paid_units=0,
            paid_cents=0,
            rejected_units=0,
            receipt_id=None,
        )

    # 回执按到达时间处理；同一 (id, line, kind) 重复出现只记一次。
    seen: set[tuple[str, str, str]] = set()
    for receipt in _receipts_until(data, as_of):
        if not receipt.line:
            if receipt.kind == "received" and receipt.batch in batches:
                batches[receipt.batch].received = True
            continue
        signature = (receipt.id, receipt.line, receipt.kind)
        if signature in seen:
            continue
        seen.add(signature)
        batch = batches.get(receipt.batch)
        if batch is None or receipt.line not in batch.lines:
            continue
        line = batch.lines[receipt.line]
        line.events.append(receipt)
        if receipt.kind == "paid" and line.status != PAID:
            line.status = PAID
            line.receipt_id = receipt.id
            claim = next(c for c in data.claims if c.id == line.transmission.claim)
            unit_price = claims_recon.terms.get(claim.service, 0)
            line.paid_units = line.transmission.units
            line.paid_cents = line.transmission.units * unit_price
        elif receipt.kind == "rejected" and line.status == SENT:
            line.status = REJECTED
            line.receipt_id = receipt.id
            line.rejected_units = line.transmission.units

    # 驳回释放：只在当前证据仍合资格时才允许再送。
    releasable: dict[str, int] = {}
    for batch in batches.values():
        for line in batch.lines.values():
            if line.status != REJECTED:
                continue
            evaluation = claims_recon.evaluations.get(line.transmission.claim)
            if evaluation is None:
                continue
            still_ok = evaluation.eligible_units + evaluation.held_units > 0
            if still_ok:
                releasable[line.transmission.claim] = (
                    releasable.get(line.transmission.claim, 0) + line.rejected_units
                )

    # 尚未送审的申报：若被标记冻结，进冻结表；已送出或历史已支付的份额不受影响。
    sent_claims = {
        line.transmission.claim
        for batch in batches.values()
        for line in batch.lines.values()
    }
    paid_claims = {p.claim for p in data.payments}
    frozen: dict[str, str] = {}
    for claim_id in sorted(frozen_claim_ids):
        if claim_id in sent_claims or claim_id in paid_claims:
            continue
        evaluation = claims_recon.evaluations.get(claim_id)
        if evaluation is None:
            continue
        frozen[claim_id] = "等待回执期间发现原票归属不实，送审行冻结"

    return TransmissionBoard(batches, releasable, frozen, as_of)


def detect_attribution_breaks(
    data: Dataset, facts: AppliedFacts, as_of: str
) -> set[str]:
    """返回截至 as_of 因更正导致原票归属不实、且尚未送审的申报 ID。"""
    broken: set[str] = set()
    changed_relations = {
        correction.supersedes
        for correction in facts.corrections
        if facts.kind.get(correction.id) == "relation-voucher"
        and correction.recorded <= as_of
    }
    changed_deliveries = {
        correction.supersedes
        for correction in facts.corrections
        if correction.recorded <= as_of
        and facts.kind.get(correction.id) in {"delivery-provider", "delivery-units"}
    }
    sent_by_as_of = {t.claim for t in data.transmissions if t.sent <= as_of}
    for claim in data.claims:
        if claim.id in sent_by_as_of:
            continue
        hint = claim.delivery_hint
        if hint in changed_deliveries:
            broken.add(claim.id)
        for relation_id in changed_relations:
            relation = facts.relations[relation_id]
            if relation.delivery == hint:
                broken.add(claim.id)
    return broken
